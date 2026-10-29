# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.4): the explicit ``pymupdf`` text-only extractor and the eight extractor-aware stack gates.

``--extractor pymupdf`` (or ``pdf.extractor: pymupdf``) reads each page's own text layer through PyMuPDF,
a core dependency, so it needs none of the PDF stack. Every test here runs with the stack genuinely
absent: ``pdf_stack.state`` reports it missing, and trip wires fail the test if anything asks to install
it (``pdf_stack.ensure``, ``_install_generation``) or touches MinerU (the server probe, the autostart,
the MinerU extraction path).

* the extractor: text pages ``pymupdf-text``, an image-only page ``unextracted``, the document method
  ``pymupdf``, every page reported through ``on_page`` with its ``page_method``, an all-image document
  failing the way the ladder fails one;
* ``auto`` never reaches it; a MinerU extractor with the stack missing fails at once naming both fixes;
* config accepts it;
* the gates: ``nx index pdf`` (a file and ``--dir``), ``nx dt index``, ``nx dt capture``, ``nx index repo``
  (including its late recheck) and ``nx collection reindex``;
* the resume decision (nexus-tr1rj.14): a ``pymupdf`` rerun over a buffer a MinerU run left, or one that
  names no rung, starts clean (the buffer is wiped), so the document is uniformly text-layer and recorded
  as ``pymupdf``; a buffer an earlier ``pymupdf`` run left is resumed.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pymupdf
import pytest
from click.testing import CliRunner

from nexus import config as nx_config
from nexus import pdf_extractor as pdfx
from nexus import pdf_stack
from nexus.cli import main
from nexus.errors import UnextractableContentError
from nexus.pdf_extractor import PDFExtractor
from nexus.pdf_stack import PdfStackMissingError
from nexus.pipeline_stages import _discard_buffer_for_text_only, _merge_buffered_provenance, extractor_loop

pytestmark = pytest.mark.usefixtures("cloud_mode")

_P1 = "The first page of this sample document discusses the history of the harbour in some detail."
_P2 = "The second page of this sample document covers the tides and the lighthouse keepers."
_P3 = "The third page of this sample document lists the ships that called at the harbour in spring."


def _make_pdf(path: Path, pages: list[str | None]) -> Path:
    """One page per entry; ``None`` is an image-only page (a picture and no text layer)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text is None:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
            pix.clear_with(120)
            page.insert_image(pymupdf.Rect(50, 50, 250, 250), pixmap=pix)
        else:
            page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


class _Trips:
    """The calls the stack-absent tests forbid. Each forbidden call raises, and is also listed."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def forbid(self, name: str):
        def _raise(*_a, **_k):
            self.calls.append(name)
            raise AssertionError(f"{name} must not be called for the pymupdf extractor")

        return _raise


@pytest.fixture
def stack_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Trips:
    """The PDF stack genuinely absent, with an install that WOULD proceed if anything asked for it."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NEXUS_PDF_PROGRESS_QUIET", "1")
    monkeypatch.setenv(pdf_stack.POLICY_ENV, pdf_stack.POLICY_INSTALL)
    monkeypatch.delenv(pdf_stack.REEXEC_ENV, raising=False)
    pdf_stack.reset_cache()
    monkeypatch.setattr(pdf_stack, "_find_spec", lambda name: False)
    monkeypatch.setattr(
        pdf_stack, "state", lambda: pdf_stack.StackState("absent", "MinerU and torch are not installed"),
    )
    monkeypatch.setattr(
        pdf_stack, "install_plan", lambda: pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND),
    )
    trips = _Trips()
    monkeypatch.setattr(pdf_stack, "ensure", trips.forbid("pdf_stack.ensure"))
    monkeypatch.setattr(pdf_stack, "_install_generation", trips.forbid("pdf_stack._install_generation"))
    monkeypatch.setattr(PDFExtractor, "_mineru_server_available", trips.forbid("_mineru_server_available"))
    monkeypatch.setattr(PDFExtractor, "_extract_with_mineru", trips.forbid("_extract_with_mineru"))
    monkeypatch.setattr(
        "nexus.daemon.mineru_lifecycle.ensure_mineru_running", trips.forbid("ensure_mineru_running"),
    )
    return trips


#: The real gate, kept before ``stack_absent`` replaces it with a trip wire: the tests that show a
#: non-pymupdf entry point still stopping put it back (:func:`_refuse_the_install`).
_REAL_ENSURE = pdf_stack.ensure


def _refuse_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over ``stack_absent``: the REAL gate, with the install refused. An explicit re-patch of the
    names the fixture changed. ``monkeypatch.undo()`` is not the way to get the real gate back:
    it also reverts the fixture's own ``NEXUS_CONFIG_DIR`` and ``HOME`` sandbox and the
    ``cloud_mode`` patches, so a refusal that records a skipped PDF writes into the real config
    directory."""
    monkeypatch.setattr(pdf_stack, "ensure", _REAL_ENSURE)
    monkeypatch.setenv(pdf_stack.POLICY_ENV, pdf_stack.POLICY_REFUSE)
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("absent", "gone"))
    monkeypatch.setattr(
        pdf_stack, "install_plan", lambda: pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND),
    )


def _skipped_record_of(config_dir: Path) -> bytes | None:
    record = config_dir / pdf_stack._SKIPPED_RECORD  # noqa: SLF001 — the file name is the stack module's
    return record.read_bytes() if record.is_file() else None


@pytest.fixture(autouse=True)
def _nothing_is_written_outside_the_sandbox(tmp_path: Path):
    """No refusal in this module records a held PDF anywhere but under the test's own tmp directory.
    A refusal that holds a PDF writes ``pdf-stack-skipped.json`` into the configuration directory, and
    ``nx doctor`` then reports a skipped PDF that never existed. The guard records the path every
    ``_skipped_path()`` call resolves (the one place that file's location is decided) and fails the test
    when one is outside ``tmp_path``. It is a ``patch`` of its own, not a ``monkeypatch``, so a test that
    calls ``monkeypatch.undo()`` (which reverts the conftest's per-test ``HOME`` and config sandbox as well
    as the fixtures' own) cannot undo the guard with it."""
    from unittest.mock import patch

    seen: list[Path] = []
    real = pdf_stack._skipped_path  # noqa: SLF001

    def spy() -> Path:
        path = real()
        seen.append(path)
        return path

    with patch.object(pdf_stack, "_skipped_path", spy):
        yield
    outside = [str(p) for p in seen if not p.is_relative_to(tmp_path)]
    assert not outside, f"a held-PDF record would be written outside the test's sandbox: {outside}"


@pytest.fixture(autouse=True)
def _forget_the_stack_verdict():
    """``pdf_stack.require`` caches a positive verdict for the process; no test here leaves one."""
    pdf_stack.reset_cache()
    yield
    pdf_stack.reset_cache()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── the extractor ────────────────────────────────────────────────────────────


def test_text_pages_are_pymupdf_text_an_image_only_page_is_unextracted_and_the_method_is_pymupdf(
    stack_absent, tmp_path,
) -> None:
    pdf = _make_pdf(tmp_path / "doc.pdf", [_P1, None, _P3])
    delivered: list[tuple[int, str, dict]] = []

    result = PDFExtractor().extract(
        pdf, extractor="pymupdf", on_page=lambda i, t, m: delivered.append((i, t, dict(m))),
    )

    assert [(i, m["page_method"], m["page_number"], m["text_length"]) for i, _t, m in delivered] == [
        (0, "pymupdf-text", 1, len(_P1)), (1, "unextracted", 2, 0), (2, "pymupdf-text", 3, len(_P3)),
    ], "every page is reported, the image-only one with empty text, each carrying its rung"
    assert [t for _i, t, _m in delivered] == [_P1, "", _P3]
    meta = result.metadata
    assert meta["extraction_method"] == "pymupdf", "not the legacy pymupdf_normalized"
    assert meta["extraction_pages"] == {"pymupdf-text": [1, 3], "unextracted": [2]}
    assert meta["page_count"] == 3
    assert meta["pages_with_text"] == [1, 3]
    assert result.text == "\n".join([_P1, "", _P3])
    # Every page has a boundary, and each one is its page's place in ``result.text`` (the page's text plus the
    # "\n" that joins it to the next page; the last page has none): the chunker attributes chunks to pages by
    # these offsets, so a wrong ``start_char`` moves every later chunk onto the wrong page.
    assert meta["page_boundaries"] == [
        {"page_number": 1, "start_char": 0, "page_text_length": len(_P1) + 1},
        {"page_number": 2, "start_char": len(_P1) + 1, "page_text_length": 1},
        {"page_number": 3, "start_char": len(_P1) + 2, "page_text_length": len(_P3)},
    ]
    for boundary, page_text in zip(meta["page_boundaries"], [_P1, "", _P3]):
        start = boundary["start_char"]
        assert result.text[start:start + boundary["page_text_length"]].rstrip("\n") == page_text
    assert stack_absent.calls == []


def test_the_extractor_reads_each_page_with_the_one_page_function(
    stack_absent, tmp_path, monkeypatch,
) -> None:
    pdf = _make_pdf(tmp_path / "doc.pdf", [_P1, _P2, _P3])
    read: list[str] = []
    real = pdfx._pymupdf_page_text

    def spy(page):
        text = real(page)
        read.append(text)
        return text

    monkeypatch.setattr(pdfx, "_pymupdf_page_text", spy)

    PDFExtractor().extract(pdf, extractor="pymupdf")

    assert read == [_P1, _P2, _P3], "one call per page, through the shared rung-3 function"
    assert stack_absent.calls == []


def test_a_document_with_no_text_layer_fails_the_way_the_ladder_fails_one(stack_absent, tmp_path) -> None:
    pdf = _make_pdf(tmp_path / "scan.pdf", [None, None])
    delivered: list[str] = []

    with pytest.raises(UnextractableContentError) as err:
        PDFExtractor().extract(pdf, extractor="pymupdf", on_page=lambda i, t, m: delivered.append(m["page_method"]))

    assert "every page of scan.pdf is unextracted" in str(err.value)
    assert "OCR" in str(err.value), "it says what to do"
    assert delivered == ["unextracted", "unextracted"], "the pages were reported before the document failed"


def test_a_damaged_page_is_unextracted_and_the_rest_of_the_document_is_still_read(
    stack_absent, tmp_path, monkeypatch,
) -> None:
    """PyMuPDF raising on one page loses that page only: it settles ``unextracted`` and the next pages are read."""
    from structlog.testing import capture_logs

    pdf = _make_pdf(tmp_path / "doc.pdf", [_P1, _P2, _P3])
    real = pdfx._pymupdf_page_text
    calls: list[int] = []

    def damaged_second_page(page):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("bad xref on page 2")
        return real(page)

    monkeypatch.setattr(pdfx, "_pymupdf_page_text", damaged_second_page)
    delivered: list[tuple[int, str]] = []

    with capture_logs() as cap:
        result = PDFExtractor().extract(
            pdf, extractor="pymupdf", on_page=lambda i, t, m: delivered.append((i, m["page_method"])),
        )

    assert delivered == [(0, "pymupdf-text"), (1, "unextracted"), (2, "pymupdf-text")], "the third page was read"
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 3], "unextracted": [2]}
    assert result.text == "\n".join([_P1, "", _P3])
    (warning,) = [e for e in cap if e["event"] == "pymupdf_page_text_layer_unreadable"]
    assert warning["page"] == 2 and "bad xref on page 2" in warning["error"]


def test_a_document_whose_every_page_pymupdf_cannot_read_is_not_told_to_be_ocred(
    stack_absent, tmp_path, monkeypatch,
) -> None:
    pdf = _make_pdf(tmp_path / "broken.pdf", [_P1, _P2])

    def unreadable(page):
        raise RuntimeError("cannot parse the content stream")

    monkeypatch.setattr(pdfx, "_pymupdf_page_text", unreadable)

    with pytest.raises(UnextractableContentError) as err:
        PDFExtractor().extract(pdf, extractor="pymupdf")

    message = str(err.value)
    assert "could not read any of the 2 page(s) of broken.pdf" in message and "cannot parse the content stream" in message
    assert "repair or re-export" in message and "OCR the PDF first" not in message


def test_a_document_mixing_unreadable_and_image_only_pages_names_both_causes(
    stack_absent, tmp_path, monkeypatch,
) -> None:
    """Page 2 raises in PyMuPDF and pages 1, 3 and 4 are real image-only pages: neither "OCR it" nor
    "repair it" alone is true of the document, so the message names both causes with their page counts."""
    pdf = _make_pdf(tmp_path / "mixed.pdf", [None, _P1, None, None])
    real = pdfx._pymupdf_page_text

    def second_page_raises(page):
        if page.number == 1:
            raise RuntimeError("bad xref on page 2")
        return real(page)

    monkeypatch.setattr(pdfx, "_pymupdf_page_text", second_page_raises)

    with pytest.raises(UnextractableContentError) as err:
        PDFExtractor().extract(pdf, extractor="pymupdf")

    message = str(err.value)
    assert "every page of mixed.pdf is unextracted" in message
    assert "PyMuPDF could not read 1 page(s)" in message and "bad xref on page 2" in message
    assert "repair or re-export" in message
    assert "3 page(s) are image-only" in message and "OCR the PDF first" in message


def test_a_password_protected_pdf_says_so_and_is_not_told_to_be_ocred(stack_absent, tmp_path) -> None:
    plain = _make_pdf(tmp_path / "plain.pdf", [_P1])
    locked = tmp_path / "locked.pdf"
    with pymupdf.open(plain) as doc:
        doc.save(str(locked), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    delivered: list[int] = []

    with pytest.raises(UnextractableContentError) as err:
        PDFExtractor().extract(locked, extractor="pymupdf", on_page=lambda i, t, m: delivered.append(i))

    assert "locked.pdf is password-protected" in str(err.value) and "OCR" not in str(err.value)
    assert delivered == [], "no page was reported"


def test_a_pdf_with_no_pages_says_so_and_is_not_told_to_be_ocred(stack_absent, tmp_path, monkeypatch) -> None:
    class _Empty:
        needs_pass = False
        metadata: dict = {}

        def __len__(self) -> int:
            return 0

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

    pdf = tmp_path / "empty.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    monkeypatch.setattr(pymupdf, "open", lambda *a, **k: _Empty())

    with pytest.raises(UnextractableContentError) as err:
        PDFExtractor().extract(pdf, extractor="pymupdf")

    assert "empty.pdf has no pages" in str(err.value) and "OCR" not in str(err.value)


@pytest.mark.parametrize(
    ("method", "text_only"), [("pymupdf", True), ("mineru", False), ("mineru+degraded", False)],
)
def test_the_quality_gate_remedy_fits_the_extractor_that_produced_the_text(method, text_only) -> None:
    """A text-only user is not sent to the stack extractor first; a MinerU user is not sent back to
    MinerU, which is what just produced the garbage."""
    from nexus.errors import ExtractionQualityError

    garbage = pdfx.ExtractionResult(
        text="x" * 400 + " " + "y" * 400, metadata={"extraction_method": method, "page_count": 1},
    )

    with pytest.raises(ExtractionQualityError) as err:
        pdfx._enforce_extraction_quality(garbage, Path("g.pdf"), allow_degraded=False)

    message = str(err.value)
    assert f"extraction_method={method}" in message and "`--allow-degraded-extraction`" in message
    assert "Remedy: retry with" not in message
    if text_only:
        assert "needs the PDF stack" in message, "`--extractor mineru` is named, with its cost, after the usable option"
    else:
        assert "--extractor mineru" not in message


_STACK_BLOCKER = """
import importlib.abc, json, sys

BLOCKED = ("mineru", "torch", "torchvision", "pdftext")
ATTEMPTS = []  # every refused import, whether or not the importer caught the ImportError


class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            ATTEMPTS.append(name)
            raise ImportError("BLOCKED stack import: " + name)
        return None


sys.meta_path.insert(0, _Block())
if sys.argv[1] == "--import":
    __import__(sys.argv[2])
    sys.exit(0)
if sys.argv[1] == "--guarded-import":
    try:
        __import__(sys.argv[2])
    except ImportError:
        pass
    print("ATTEMPTS=" + json.dumps(ATTEMPTS))
    sys.exit(0)
from pathlib import Path

from nexus.pdf_extractor import PDFExtractor

result = PDFExtractor().extract(Path(sys.argv[1]), extractor="pymupdf")
print("METHOD=" + result.metadata["extraction_method"] + " PAGES=" + str(result.metadata["page_count"]))
print("ATTEMPTS=" + json.dumps(ATTEMPTS))
"""


def _run_blocked(tmp_path: Path, *args: str) -> tuple[int, str, str]:
    """Run ``_STACK_BLOCKER`` in a fresh interpreter, in its own process group (stopped on a timeout)."""
    import os
    import subprocess
    import sys

    from tests._child_process import popen_in_group, stop_group

    env = {
        **os.environ, "NEXUS_CONFIG_DIR": str(tmp_path / "cfg"), "HOME": str(tmp_path), "NEXUS_NO_TELEMETRY": "1",
    }
    proc = popen_in_group(
        [sys.executable, "-c", _STACK_BLOCKER, *args],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        out, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        stop_group(proc)
        raise
    finally:
        stop_group(proc)
    return proc.returncode, out, err


def test_the_pymupdf_path_imports_none_of_the_pdf_stack_even_where_it_is_installed(tmp_path) -> None:
    """The in-process tests simulate an absent stack by patching ``pdf_stack.state``; the stack's packages
    are still importable in this venv, so a stray ``import torch`` in the pymupdf path would pass them.
    Here a fresh interpreter has a meta-path finder that refuses mineru, torch, torchvision and
    pdftext: importing the extractor module and extracting a PDF with ``pymupdf`` must not ask for any."""
    pdf = _make_pdf(tmp_path / "doc.pdf", [_P1, None, _P3])

    code, out, err = _run_blocked(tmp_path, str(pdf))

    assert code == 0, err[-2000:]
    assert "METHOD=pymupdf PAGES=3" in out
    assert "ATTEMPTS=[]" in out, (
        "the finder records every refused import, so one wrapped in try/except ImportError shows here too: " + out
    )


def test_the_attempt_record_sees_a_stack_import_the_importer_swallowed(tmp_path) -> None:
    """The control for the record: a guarded ``import torch`` leaves the process healthy (exit 0) and is
    visible only in the attempt list, which is what the test above asserts is empty."""
    code, out, err = _run_blocked(tmp_path, "--guarded-import", "torch")
    assert code == 0, err[-2000:]
    assert 'ATTEMPTS=["torch"]' in out


@pytest.mark.parametrize("module", ["mineru", "torch", "torchvision", "pdftext"])
def test_the_stack_blocker_refuses_each_module_it_names(tmp_path, module) -> None:
    """The control for the test above: a blocker that blocked nothing would let it pass over a stray import."""
    code, _out, err = _run_blocked(tmp_path, "--import", module)
    assert code != 0 and f"BLOCKED stack import: {module}" in err


@pytest.mark.parametrize("extractor", ["auto", "mineru"])
def test_an_extractor_that_needs_the_stack_fails_at_once_naming_both_fixes_and_extracts_no_page(
    stack_absent, tmp_path, extractor,
) -> None:
    pdf = _make_pdf(tmp_path / "doc.pdf", [_P1, _P2])
    pages: list[int] = []

    with pytest.raises(PdfStackMissingError) as err:
        PDFExtractor().extract(pdf, extractor=extractor, on_page=lambda i, t, m: pages.append(i))

    text = str(err.value)
    assert pdf_stack.repair_command() in text, "fix 1: install the PDF stack"
    assert "extract with the `pymupdf` extractor" in text and "--extractor" not in text, (
        "fix 2: text only, named for a library call (no flag or setting of its own)"
    )
    assert pages == [], "no page was extracted, and nothing degraded to its text layer on its own"
    assert stack_absent.calls == []


@pytest.mark.parametrize("name", ["auto", "mineru"])
def test_no_name_but_pymupdf_chooses_the_text_only_extractor(stack_absent, tmp_path, monkeypatch, name) -> None:
    """The older names run MinerU; none of the routes is the explicit text-only extractor."""
    pdf = _make_pdf(tmp_path / "doc.pdf", [_P1, _P2])
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("present", ""))
    pdf_stack.reset_cache()
    monkeypatch.setattr(PDFExtractor, "_extract_pymupdf", stack_absent.forbid("_extract_pymupdf"))
    fake = pdfx.ExtractionResult(
        text="x " * 40, metadata={"extraction_method": "mineru", "page_count": 1, "page_boundaries": []},
    )
    monkeypatch.setattr(PDFExtractor, "_extract_with_mineru", lambda self, *a, **k: fake)

    result = PDFExtractor().extract(pdf, extractor=name, allow_degraded=True)

    assert result.metadata["extraction_method"] == "mineru"
    assert "_extract_pymupdf" not in stack_absent.calls


def test_pdf_extractor_config_accepts_pymupdf_and_nothing_chooses_it_by_default(monkeypatch) -> None:
    def cfg(pdf: dict | None):
        monkeypatch.setattr(nx_config, "load_config", lambda **kw: {"pdf": pdf} if pdf is not None else {})
        return nx_config.get_pdf_extractor()

    assert cfg({"extractor": "pymupdf"}) == "pymupdf"
    assert cfg(None) == "mineru", "the default is never the text-only extractor"
    assert cfg({"extractor": "Pymupdf-ish"}) == "mineru", "an unknown value falls back to the default"


@pytest.mark.parametrize("command", [["index", "pdf"], ["dt", "index"], ["dt", "capture"]])
def test_the_three_clis_offer_pymupdf_and_none_defaults_to_it(command) -> None:
    from click import Choice

    node = main
    for part in command:
        node = node.commands[part]
    (option,) = [p for p in node.params if p.name == "extractor"]
    assert isinstance(option.type, Choice) and "pymupdf" in option.type.choices
    assert option.default != "pymupdf"


# ── the stack module's side of it ────────────────────────────────────────────


def test_needs_stack_is_false_only_for_pymupdf() -> None:
    assert pdf_stack.needs_stack("pymupdf") is False
    for other in ("", "auto", "mineru", "PYMUPDF", "anything"):
        assert pdf_stack.needs_stack(other) is True


def test_require_and_ensure_or_exit_let_pymupdf_by_with_the_stack_absent_and_refuse_the_rest(
    stack_absent, monkeypatch,
) -> None:
    pdf_stack.require("x", extractor="pymupdf")
    assert pdf_stack.ensure_or_exit("x", extractor="pymupdf") is True
    assert stack_absent.calls == [], "no install attempt, no state check that could start one"

    for other in ("auto", "mineru", ""):
        with pytest.raises(PdfStackMissingError):
            pdf_stack.require("x", extractor=other)
    # control: the real ensure, with the install refused, still stops a non-pymupdf entry point
    _refuse_the_install(monkeypatch)
    with pytest.raises(SystemExit) as exit_:
        pdf_stack.ensure_or_exit("x", extractor="mineru")
    assert exit_.value.code == pdf_stack.EXIT_REFUSED


# The text-only sentence each class of entry point is given, spelled out here (not computed from the
# stack module) so a wrong wording fails. A command can only be sent to a flag or a setting it reads.
_FLAG_AND_SETTING = (
    "For text only (no layout, tables or math), rerun with `--extractor pymupdf` "
    "(or set `pdf.extractor: pymupdf`): that needs no PDF support."
)
_SETTING_ONLY = (
    "For text only (no layout, tables or math), set `pdf.extractor: pymupdf` and rerun: "
    "that needs no PDF support."
)
_NO_SENTENCE = None  # `nx mineru start` starts the MinerU server: there is no extractor to choose

_ENTRY_CLASSES = [
    pytest.param("nx index pdf", _FLAG_AND_SETTING, id="flag-and-setting:index-pdf"),
    pytest.param("nx index pdf --dir", _FLAG_AND_SETTING, id="flag-and-setting:index-pdf-dir"),
    pytest.param("nx index repo", _SETTING_ONLY, id="setting-only:index-repo"),
    pytest.param("nx collection reindex", _SETTING_ONLY, id="setting-only:collection-reindex"),
    pytest.param("nx dt index", _FLAG_AND_SETTING, id="flag-and-setting:dt-index"),
    pytest.param("nx dt capture", _FLAG_AND_SETTING, id="flag-and-setting:dt-capture"),
    pytest.param("nx mineru start", _NO_SENTENCE, id="none:mineru-start"),
]


def _assert_names_the_way_out(message: str, expected: str | None) -> None:
    if expected is None:
        assert "pymupdf" not in message and "For text only" not in message, message
    else:
        assert message.count(expected) == 1 and message.count("For text only") == 1, message
        if expected is _FLAG_AND_SETTING:
            assert "--extractor pymupdf" in message and "pdf.extractor: pymupdf" in message, "both ways are named"
        if expected is _SETTING_ONLY:
            assert "--extractor" not in message, "`nx index repo` and `nx collection reindex` have no flag"


@pytest.fixture
def refusal_bed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """The REAL ``ensure``, ``require`` and ``_remedy`` over a box where the stack is absent and the install
    would proceed; each refusal test then breaks one thing."""
    import contextlib

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(pdf_stack.POLICY_ENV, pdf_stack.POLICY_INSTALL)
    monkeypatch.delenv(pdf_stack.REEXEC_ENV, raising=False)
    pdf_stack.reset_cache()
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("absent", "gone"))
    monkeypatch.setattr(
        pdf_stack, "install_plan", lambda: pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND),
    )
    monkeypatch.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: None)
    monkeypatch.setattr(pdf_stack, "_install_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pdf_stack, "current_generation_has_stack", lambda: False)
    monkeypatch.setattr(pdf_stack, "_current_generation_receipt", lambda: None)
    return monkeypatch


def _installer_fails(message: str):
    def _raise(*_a, **_k):
        raise RuntimeError(message)

    return _raise


def _newer_generation(mp: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    mp.setattr(pdf_stack, "_running_version", lambda: "1.0.0")
    receipt = SimpleNamespace(version="9.9.9", source_kind="directory", source="/a/checkout", extras=())
    mp.setattr(pdf_stack, "_current_generation_receipt", lambda: (Path("gen"), receipt))


def _windows_gap_with_the_stack_present(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("present", ""))
    mp.setattr(pdf_stack, "_windows_runtime_gap", lambda *a, **k: ("none", None))


#: The ways ``ensure`` refuses or fails, each as a patch over ``refusal_bed``.
_REFUSAL_KINDS = {
    "stack-absent-and-the-install-refused": lambda mp: mp.setenv(pdf_stack.POLICY_ENV, pdf_stack.POLICY_REFUSE),
    "install-not-automatic-here": lambda mp: mp.setattr(
        pdf_stack, "install_plan", lambda: pdf_stack.InstallPlan("directory", False, "scripts/reinstall-tool.sh"),
    ),
    "windows-runtime-gap-before-an-install": lambda mp: mp.setattr(
        pdf_stack, "_windows_runtime_gap", lambda *a, **k: ("none", None),
    ),
    "windows-runtime-gap-over-an-installed-stack": _windows_gap_with_the_stack_present,
    "newer-generation-built-from-a-checkout": _newer_generation,
    "installer-network-failure": lambda mp: mp.setattr(
        pdf_stack, "_install_generation", _installer_fails("tcp connect error: connection refused"),
    ),
    "installer-failure": lambda mp: mp.setattr(
        pdf_stack, "_install_generation", _installer_fails("no solution found when resolving dependencies"),
    ),
    "reexec-guard": lambda mp: mp.setenv(pdf_stack.REEXEC_ENV, "1"),
}


def _refusal_message(entry: str) -> str:
    with pytest.raises(pdf_stack.PdfStackError) as err:
        pdf_stack.ensure(entry)
    return str(err.value)


@pytest.mark.parametrize("kind", list(_REFUSAL_KINDS))
def test_every_kind_of_refusal_names_the_text_only_way_out_in_the_words_of_the_command_that_refused(
    refusal_bed, kind,
) -> None:
    """The stack-absent message, the Windows runtime gap, the newer-generation refusal, the installer's
    network and other failures and the re-exec guard: each carries the sentence, once, and not the
    flag to a command that has none (or the setting to one that does not read it)."""
    _REFUSAL_KINDS[kind](refusal_bed)

    _assert_names_the_way_out(_refusal_message("nx index pdf"), _FLAG_AND_SETTING)
    _assert_names_the_way_out(_refusal_message("nx index repo"), _SETTING_ONLY)
    _assert_names_the_way_out(_refusal_message("nx dt capture"), _FLAG_AND_SETTING)
    _assert_names_the_way_out(_refusal_message("nx mineru start"), _NO_SENTENCE)


@pytest.mark.parametrize(("entry", "expected"), _ENTRY_CLASSES)
def test_each_class_of_entry_point_is_sent_to_the_flag_or_setting_it_reads(refusal_bed, entry, expected) -> None:
    refusal_bed.setenv(pdf_stack.POLICY_ENV, pdf_stack.POLICY_REFUSE)
    _assert_names_the_way_out(_refusal_message(entry), expected)
    # the same sentence from the other two places the missing-stack text is built
    _assert_names_the_way_out(pdf_stack.missing_message(entry) or "", expected)
    with pytest.raises(PdfStackMissingError) as err:
        pdf_stack.require(entry)
    _assert_names_the_way_out(str(err.value), expected)


def test_the_library_backstops_are_told_to_pass_the_extractor_by_name(refusal_bed) -> None:
    """``index_pdf`` and ``PDFExtractor.extract`` are library calls: no flag, no setting of their own."""
    for entry in ("index_pdf", "extracting doc.pdf", "PDF doc.pdf"):
        with pytest.raises(PdfStackMissingError) as err:
            pdf_stack.require(entry)
        message = str(err.value)
        assert "`extractor=\"pymupdf\"`" in message and "--extractor" not in message and "pdf.extractor" not in message


def test_a_windows_gap_refusal_from_require_names_the_way_out_too(refusal_bed) -> None:
    _windows_gap_with_the_stack_present(refusal_bed)
    with pytest.raises(PdfStackMissingError) as err:
        pdf_stack.require("nx dt index")
    assert "Visual C++" in str(err.value)
    _assert_names_the_way_out(str(err.value), _FLAG_AND_SETTING)


def test_every_cli_gate_names_an_entry_the_remedy_table_knows() -> None:
    """The table that decides the wording is keyed on the ``entry`` string each gate passes. A gate that
    renames its entry, or a new one, falls to the library wording and tells a CLI user to pass a keyword."""
    from tests.test_pdf_stack_entry_point_census import _gate_calls

    calls = _gate_calls()
    cli = {label for _rel, _line, label, _aware in calls if label.startswith("nx ")}
    assert cli == set(pdf_stack.ENTRY_EXTRACTOR_CHOICE), sorted(cli ^ set(pdf_stack.ENTRY_EXTRACTOR_CHOICE))
    assert len(cli) >= 7


# ── nx index pdf ─────────────────────────────────────────────────────────────


def _reporting(report: dict | None):
    """A fake ``index_pdf`` that records its calls and reports ``report`` through ``on_extraction``."""
    calls: list[dict] = []

    def fake(path, **kwargs):
        calls.append(kwargs)
        sink = kwargs.get("on_extraction")
        if sink is not None and report is not None:
            sink(report)
        if kwargs.get("return_metadata"):
            return {"chunks": 5, "pages": [1], "title": "", "author": "", "page_count": 3, "pages_with_text": [1, 3]}
        return 5

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


_REPORT = {"extraction_method": "pymupdf", "extraction_pages": {"pymupdf-text": [1, 3], "unextracted": [2]}}


@pytest.mark.parametrize("tty", [False, True], ids=["metadata-branch", "plain-branch"])
def test_index_pdf_with_pymupdf_installs_nothing_and_the_summary_names_the_extractor(
    stack_absent, runner, tmp_path, tty,
) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    fake = _reporting(_REPORT)
    with patch("nexus.doc_indexer.index_pdf", side_effect=fake), \
            patch("click.testing._NamedTextIOWrapper.isatty", lambda self: tty):
        result = runner.invoke(main, ["index", "pdf", str(pdf), "--extractor", "pymupdf"])

    assert result.exit_code == 0, result.output
    assert fake.calls[0]["extractor"] == "pymupdf"
    assert "Extractor: pymupdf, text layer only" in result.output
    assert "  a.pdf: no text layer, not extracted: page(s) 2" in result.output
    assert "Degraded pages" not in result.output, "a text-only run is not a fall-back"
    assert stack_absent.calls == []


#: A ``--extractor pymupdf`` rerun over pages a MinerU run left: the catalog records the mixture.
_MIXTURE = {
    "extraction_method": "mineru+degraded",
    "extraction_pages": {"pymupdf-text": [3, 4, 5], "unextracted": [6]},
}


def test_the_summary_follows_the_recorded_method_so_a_mixture_is_never_called_text_only(
    stack_absent, runner, tmp_path,
) -> None:
    """Asked for ``pymupdf``, recorded ``mineru+degraded``: the summary must not say "text layer only"."""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    with patch("nexus.doc_indexer.index_pdf", side_effect=_reporting(_MIXTURE)):
        result = runner.invoke(main, ["index", "pdf", str(pdf), "--extractor", "pymupdf"])

    assert result.exit_code == 0, result.output
    assert "Extractor: pymupdf" not in result.output and "text layer only" not in result.output
    assert "Degraded pages in 1 document(s)" in result.output
    assert "  a.pdf: pymupdf-text 3,4,5; unextracted 6" in result.output, "the text-layer pages are listed"


def test_a_directory_run_prints_each_form_for_the_documents_it_applies_to(stack_absent, runner, tmp_path) -> None:
    d = tmp_path / "papers"
    d.mkdir()
    for name in ("clean.pdf", "mixed.pdf"):
        (d / name).write_bytes(b"%PDF-1.4 x")
    inner = _reporting(None)

    def fake(path, **kwargs):
        sink = kwargs.get("on_extraction")
        sink(_MIXTURE if Path(path).name == "mixed.pdf" else _REPORT)
        return inner(path, **kwargs)

    with patch("nexus.doc_indexer.index_pdf", side_effect=fake):
        result = runner.invoke(main, ["index", "pdf", "--dir", str(d), "--extractor", "pymupdf"])

    assert result.exit_code == 0, result.output
    assert "Extractor: pymupdf, text layer only (no layout, tables or math), 1 document(s)." in result.output
    assert "  clean.pdf: no text layer, not extracted: page(s) 2" in result.output
    assert "Degraded pages in 1 document(s)" in result.output
    assert "  mixed.pdf: pymupdf-text 3,4,5; unextracted 6" in result.output
    assert "clean.pdf: pymupdf-text" not in result.output, "the text-only document is not also listed as degraded"


def test_index_pdf_with_the_config_set_to_pymupdf_and_no_flag_installs_nothing(
    stack_absent, runner, tmp_path, monkeypatch,
) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    real = nx_config.load_config
    monkeypatch.setattr(nx_config, "load_config", lambda **kw: {**real(**kw), "pdf": {"extractor": "pymupdf"}})
    fake = _reporting(_REPORT)
    with patch("nexus.doc_indexer.index_pdf", side_effect=fake):
        result = runner.invoke(main, ["index", "pdf", str(pdf)])
    assert result.exit_code == 0, result.output
    assert fake.calls[0]["extractor"] == "pymupdf"
    assert stack_absent.calls == []


def test_index_pdf_with_mineru_and_the_stack_missing_stops_and_names_both_fixes(
    stack_absent, runner, tmp_path, monkeypatch,
) -> None:
    _refuse_the_install(monkeypatch)  # the real gate, the install refused
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    with patch("nexus.doc_indexer.index_pdf") as index_pdf:
        result = runner.invoke(main, ["index", "pdf", str(pdf), "--extractor", "mineru"])
    assert result.exit_code == pdf_stack.EXIT_REFUSED, result.output
    assert pdf_stack.INSTALL_COMMAND in result.output
    assert _FLAG_AND_SETTING in result.output, "`nx index pdf` has the flag and reads the setting"
    index_pdf.assert_not_called()


def test_index_pdf_dir_with_pymupdf_installs_nothing_and_never_probes_mineru(
    stack_absent, runner, tmp_path,
) -> None:
    d = tmp_path / "papers"
    d.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (d / name).write_bytes(b"%PDF-1.4 x")
    fake = _reporting(_REPORT)
    with patch("nexus.doc_indexer.index_pdf", side_effect=fake):
        result = runner.invoke(main, ["index", "pdf", "--dir", str(d), "--extractor", "pymupdf"])

    assert result.exit_code == 0, result.output
    assert [c["extractor"] for c in fake.calls] == ["pymupdf", "pymupdf"]
    assert "Extractor: pymupdf, text layer only (no layout, tables or math), 2 document(s)." in result.output
    assert "  a.pdf: no text layer, not extracted: page(s) 2" in result.output
    assert "MinerU server" not in result.output, "no probe, so neither of the probe's two lines"
    assert stack_absent.calls == [], "the probe (and with it a MinerU autostart) never ran"


def test_index_pdf_dir_with_mineru_does_probe_the_server(runner, tmp_path, monkeypatch) -> None:
    """The control for the test above: the probe is live for the extractor that needs MinerU."""
    d = tmp_path / "papers"
    d.mkdir()
    (d / "a.pdf").write_bytes(b"%PDF-1.4 x")
    probes: list[int] = []
    monkeypatch.setattr(pdf_stack, "ensure", lambda entry: pdf_stack.Outcome.PRESENT)
    monkeypatch.setattr(PDFExtractor, "_mineru_server_available", lambda self: probes.append(1) or False)
    with patch("nexus.doc_indexer.index_pdf", side_effect=_reporting(None)):
        result = runner.invoke(main, ["index", "pdf", "--dir", str(d), "--extractor", "mineru"])
    assert result.exit_code == 0, result.output
    assert probes == [1] and "MinerU server not running" in result.output


# ── nx dt index and nx dt capture ────────────────────────────────────────────


def _isolate_dt(monkeypatch, records):
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: records)
    monkeypatch.setattr("nexus.commands.dt._stamp_dt_uri_on_entry", lambda *a, **kw: True)
    monkeypatch.setattr("nexus.commands.dt._dt_uri_is_catalogued", lambda uuid: False)
    monkeypatch.setattr("nexus.commands.dt._dt_record_facts", lambda uuid: None)


def test_dt_index_with_pymupdf_installs_nothing_and_the_summary_names_the_extractor(
    stack_absent, runner, monkeypatch,
) -> None:
    _isolate_dt(monkeypatch, [("U1", "/x/a.pdf")])
    fake = _reporting(_REPORT)
    with patch("nexus.doc_indexer.index_pdf", side_effect=fake):
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1", "--extractor", "pymupdf", "--allow-page-gap"])
    assert result.exit_code == 0, result.output
    assert fake.calls[0]["extractor"] == "pymupdf"
    assert "Extractor: pymupdf, text layer only (no layout, tables or math), 1 document(s)." in result.output
    assert "  a.pdf (U1): no text layer, not extracted: page(s) 2" in result.output
    assert "Degraded pages" not in result.output
    assert stack_absent.calls == []


def test_dt_index_summary_follows_the_recorded_method_too(stack_absent, runner, monkeypatch) -> None:
    _isolate_dt(monkeypatch, [("U1", "/x/a.pdf")])
    with patch("nexus.doc_indexer.index_pdf", side_effect=_reporting(_MIXTURE)):
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1", "--extractor", "pymupdf", "--allow-page-gap"])
    assert result.exit_code == 0, result.output
    assert "text layer only" not in result.output
    assert "Degraded pages in 1 document(s)" in result.output
    assert "  a.pdf (U1): pymupdf-text 3,4,5; unextracted 6" in result.output


def test_dt_page_coverage_failure_for_pymupdf_says_an_image_page_is_expected_and_does_not_send_to_mineru(
    stack_absent, runner, monkeypatch,
) -> None:
    _isolate_dt(monkeypatch, [("U1", "/x/a.pdf")])
    monkeypatch.setattr("nexus.commands.dt._dt_record_facts", lambda uuid: {"name": "A", "page_count": 3})
    monkeypatch.setattr("nexus.commands.dt._stamp_page_gap", lambda uuid, missing: None)
    with patch("nexus.doc_indexer.index_pdf", side_effect=_reporting(_REPORT)):
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1", "--extractor", "pymupdf"])
        monkeypatch.setattr(pdf_stack, "ensure", lambda entry: pdf_stack.Outcome.PRESENT)  # control: the stack is there
        control = runner.invoke(main, ["dt", "index", "--uuid", "U1", "--extractor", "mineru"])
    assert "page coverage: 2 of 3 pages produced text" in result.output
    assert "an image page is expected to be missing" in result.output and "--allow-page-gap" in result.output
    assert "Re-run with --extractor mineru" not in result.output
    assert "Re-run with --extractor mineru, or --allow-page-gap to accept." in control.output, "other extractors keep it"


def test_dt_index_with_mineru_and_the_stack_missing_still_stops(stack_absent, runner, monkeypatch) -> None:
    _refuse_the_install(monkeypatch)
    _isolate_dt(monkeypatch, [("U1", "/x/a.pdf")])
    with patch("nexus.doc_indexer.index_pdf") as index_pdf:
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1", "--extractor", "mineru"])
    assert result.exit_code == pdf_stack.EXIT_REFUSED, result.output
    assert _FLAG_AND_SETTING in result.output, "`nx dt` reads the flag and the setting"
    index_pdf.assert_not_called()


def test_mineru_start_with_the_stack_missing_stops_and_offers_no_text_only_way_out(
    stack_absent, runner, monkeypatch,
) -> None:
    """The one gate with no extractor to choose: starting the MinerU server needs the stack."""
    _refuse_the_install(monkeypatch)
    result = runner.invoke(main, ["mineru", "start"])
    assert result.exit_code == pdf_stack.EXIT_REFUSED, result.output
    assert pdf_stack.INSTALL_COMMAND in result.output
    assert "pymupdf" not in result.output and "For text only" not in result.output


@pytest.fixture
def capture_bed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("nexus.commands.dt._is_darwin", lambda: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)

    class _Bed:
        touched: list[str] = []
        indexed_with: list[dict] = []

    bed = _Bed()
    bed.touched, bed.indexed_with = [], []
    monkeypatch.setattr(
        "nexus.mcp_client.devonthink.dt_import_file", lambda *a, **kw: bed.touched.append("import_file") or "NEW-UUID",
    )
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: [(kw["uuids"][0], "/captured.pdf")])

    def _index_record(uuid, path, **kw):
        bed.indexed_with.append(kw)
        return (True, 1)

    monkeypatch.setattr("nexus.commands.dt._index_record", _index_record)
    return bed


def test_dt_capture_of_a_pdf_with_pymupdf_installs_nothing_and_indexes_as_text(
    stack_absent, runner, capture_bed,
) -> None:
    result = runner.invoke(main, ["dt", "capture", "--file", "/tmp/paper.pdf", "--extractor", "pymupdf"])
    assert result.exit_code == 0, result.output
    assert capture_bed.touched == ["import_file"]
    assert [kw["extractor"] for kw in capture_bed.indexed_with] == ["pymupdf"]
    assert stack_absent.calls == []


# ── nx index repo ────────────────────────────────────────────────────────────

_REPO_CONFIG = {
    "server": {"ignorePatterns": []},
    "indexing": {"code_extensions": [], "prose_extensions": [], "rdr_paths": ["docs/rdr"], "include_untracked": False},
    "pdf": {"extractor": "pymupdf"},
}


def _repo_doubles(config: dict):
    from contextlib import ExitStack

    from nexus.db.http_vector_client import HttpVectorClient

    db = MagicMock(spec=HttpVectorClient)
    stack = ExitStack()
    mocks: dict[str, MagicMock] = {}
    patches = {
        "nexus.frecency.batch_frecency": {"return_value": {}},
        "nexus.indexer._git_metadata": {"return_value": {}},
        "nexus.config.load_config": {"return_value": config},
        "nexus.config.get_credential": {"return_value": "fake-key"},
        "nexus.mcp_infra.get_t3": {"return_value": db},
        "nexus.db.make_t3": {"return_value": db},
        "nexus.indexer._index_code_file": {"return_value": 1},
        "nexus.indexer._index_prose_file": {"return_value": 0},
        "nexus.indexer._index_pdf_file": {"return_value": 1},
        "nexus.indexer._prune_misclassified": {},
        "nexus.indexer._prune_deleted_files": {},
        "nexus.indexer._migrate_legacy_collections": {"return_value": {}},
        "nexus.catalog.factory.make_catalog_reader": {"return_value": None},
        "nexus.catalog.factory.make_catalog_writer": {"return_value": None},
        "nexus.indexer._catalog_hook": {
            "side_effect": lambda **kw: {p: f"1.1.{i}" for i, (p, _t, _c) in enumerate(kw["indexed_files"], 1)},
        },
        "nexus.corpus.ensure_collection_registered": {},
        # the late recheck's staleness test: every PDF reads as stale, so a recheck that ran would hold it
        "nexus.indexer._pdf_is_current": {"return_value": False},
    }
    for target, kw in patches.items():
        mocks[target.rsplit(".", 1)[-1]] = stack.enter_context(patch(target, **kw))
    return stack, mocks


def _registry() -> MagicMock:
    reg = MagicMock()
    reg.get.return_value = {
        "collection": "code__repo",
        "code_collection": "code__repo__voyage-code-3__v1",
        "docs_collection": "docs__repo__voyage-context-3__v1",
    }
    return reg


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    (r / "ok.py").write_text("x = 1\n")
    (r / "paper.pdf").write_bytes(b"%PDF-1.4 fake content")
    return r


def test_index_repo_with_the_config_set_to_pymupdf_holds_nothing_installs_nothing_and_extracts_as_text(
    stack_absent, repo, monkeypatch,
) -> None:
    from nexus.indexer import _run_index

    state_reads: list[int] = []
    monkeypatch.setattr(
        pdf_stack, "state",
        lambda: state_reads.append(1) or pdf_stack.StackState("absent", "gone"),
    )
    stack, mocks = _repo_doubles(_REPO_CONFIG)
    with stack:
        stats = _run_index(repo, _registry())

    assert stats["pdf_stack_held"] == 0
    (call,) = mocks["_index_pdf_file"].call_args_list
    assert call.args[0] == repo / "paper.pdf"
    assert call.kwargs["extractor"] == "pymupdf", "the repo's pdf.extractor reaches the PDF indexer"
    assert pdf_stack.skipped_count() == 0
    assert state_reads == [], "neither the early hold's probe nor the late recheck read the stack"
    assert stack_absent.calls == []


def test_index_repo_without_the_setting_still_holds_the_pdf_when_the_stack_is_absent(
    stack_absent, repo, monkeypatch, tmp_path,
) -> None:
    """The control: same repo and doubles, no ``pdf.extractor``, the install refused: the PDF is held,
    and the record of the held PDF lands in the test's own config directory, not the real one."""
    from nexus.indexer import _run_index

    _refuse_the_install(monkeypatch)
    config = {k: v for k, v in _REPO_CONFIG.items() if k != "pdf"}
    stack, mocks = _repo_doubles(config)
    with stack:
        stats = _run_index(repo, _registry())
    assert stats["pdf_stack_held"] == 1
    mocks["_index_pdf_file"].assert_not_called()
    assert _skipped_record_of(tmp_path / "cfg") is not None, "the held PDF was recorded, inside the sandbox"


def test_the_repo_gate_lets_pymupdf_by_without_probing_the_stack_or_recording_a_skip(stack_absent, tmp_path) -> None:
    from nexus.indexer import _hold_pdfs_without_stack

    probe = MagicMock(side_effect=AssertionError("the probe asks whether the stack is absent"))
    held = _hold_pdfs_without_stack(
        [tmp_path / "a.pdf"], tmp_path, probe=probe, extractor="pymupdf",
    )
    assert held == []
    probe.assert_not_called()
    assert stack_absent.calls == []


# ── nx collection reindex ────────────────────────────────────────────────────


@pytest.fixture
def reindex_bed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from nexus.db.collection_purge import CascadeCounts
    from nexus.db.http_vector_client import HttpVectorClient

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    class _Bed:
        pass

    bed = _Bed()
    bed.pdf = pdf
    bed.db = MagicMock(spec=HttpVectorClient)
    bed.db.collection_info.side_effect = lambda _name: {"count": 4, "metadata": {}}
    col = MagicMock()
    col.get.side_effect = lambda **_kw: {"ids": ["id0"], "metadatas": [{"source_path": str(pdf)}]}
    bed.db.get_or_create_collection.return_value = col
    bed.purge = MagicMock(return_value=CascadeCounts())
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: MagicMock())
    monkeypatch.setattr("nexus.catalog.tombstones.read_tombstones", lambda cat: [])
    monkeypatch.setattr("nexus.catalog.tombstones.deleted_only_sources", lambda cat, tombs, paths, collection=None: [])
    monkeypatch.setattr("nexus.db.collection_purge.purge_collection_cascade", bed.purge)
    monkeypatch.setattr("nexus.commands.collection._t3", lambda: bed.db)
    monkeypatch.setattr("nexus.commands.index.run_collection_postprocessing", lambda *a, **k: None)
    monkeypatch.setattr("nexus.corpus.discard_cached_registration", lambda name: None)
    monkeypatch.setattr("nexus.corpus.ensure_collection_registered", lambda *a, **k: None)
    monkeypatch.setattr("nexus.db.t3.verify_collection_deep", MagicMock())
    return bed


def test_collection_reindex_with_the_config_set_to_pymupdf_installs_nothing_and_re_extracts_as_text(
    stack_absent, runner, reindex_bed, monkeypatch,
) -> None:
    real = nx_config.load_config
    monkeypatch.setattr(nx_config, "load_config", lambda **kw: {**real(**kw), "pdf": {"extractor": "pymupdf"}})
    with patch("nexus.doc_indexer.index_pdf", return_value=3) as index_pdf:
        result = runner.invoke(main, ["collection", "reindex", "docs__papers", "--force"])

    assert result.exit_code == 0, result.output
    reindex_bed.purge.assert_called_once()
    (call,) = index_pdf.call_args_list
    assert call.kwargs["extractor"] == "pymupdf", "the setting reaches index_pdf"
    assert stack_absent.calls == []


# ── the resume decision: a pymupdf rerun over a buffer MinerU left ───────────


_HASH = "h" * 64


def test_the_merge_records_a_pymupdf_rerun_over_a_minerus_buffer_as_a_mixture() -> None:
    """A breaker trip keeps the buffer, so a ``--extractor pymupdf`` rerun meets pages that are MinerU's.
    Pages 1 and 2 are MinerU's (the kept buffer), pages 3 to 5 are the text layer's: ``pymupdf`` would call the whole document text-only,
    so the method is the ladder's own ``mineru+degraded`` and the map lists the text-layer pages."""
    meta = {"extraction_method": "pymupdf", "extraction_pages": {"pymupdf-text": [1, 2, 3, 4, 5]}}
    run = {n: "pymupdf-text" for n in range(1, 6)}
    _merge_buffered_provenance(meta, run, {1: "mineru", 2: "mineru"})
    assert meta == {"extraction_method": "mineru+degraded", "extraction_pages": {"pymupdf-text": [3, 4, 5]}}


def test_the_merge_records_a_mineru_no_formula_buffer_page_as_a_mixture_too() -> None:
    meta = {"extraction_method": "pymupdf", "extraction_pages": {"pymupdf-text": [1, 2]}}
    _merge_buffered_provenance(meta, {1: "pymupdf-text", 2: "pymupdf-text"}, {1: "mineru-no-formula"})
    assert meta["extraction_method"] == "mineru+degraded"
    assert meta["extraction_pages"] == {"mineru-no-formula": [1], "pymupdf-text": [2]}


def test_the_merge_keeps_pymupdf_for_a_buffer_an_earlier_pymupdf_run_left() -> None:
    meta = {"extraction_method": "pymupdf", "extraction_pages": {"pymupdf-text": [1, 3, 4], "unextracted": [2]}}
    run = {1: "pymupdf-text", 2: "unextracted", 3: "pymupdf-text", 4: "pymupdf-text"}
    _merge_buffered_provenance(meta, run, {1: "pymupdf-text", 2: "unextracted"})
    assert meta == {
        "extraction_method": "pymupdf",
        "extraction_pages": {"pymupdf-text": [1, 3, 4], "unextracted": [2]},
    }


def test_the_merge_cannot_call_a_buffer_page_with_no_rung_text_only() -> None:
    meta = {"extraction_method": "pymupdf", "extraction_pages": {"pymupdf-text": [1, 2, 3]}}
    _merge_buffered_provenance(meta, {1: "pymupdf-text", 2: "pymupdf-text", 3: "pymupdf-text"}, {1: "pymupdf-text"}, [2])
    assert meta["extraction_method"] == "mineru+degraded", "page 2's buffered text may be another extractor's"
    assert meta["extraction_pages"] == {"pymupdf-text": [1, 3]}, "the unproven page is left out of the map"


def test_the_merge_leaves_the_runs_own_map_alone_for_a_first_pymupdf_run() -> None:
    """With nothing buffered the run's own record stands. The run's rungs here deliberately differ from its map
    (page 2 is in the map but not in the rungs): a merge that recomputed the map from the rungs, which is what
    dropping the early return does, would lose page 2, so this fails then. Equal inputs could not tell the two apart."""
    meta = {"extraction_method": "pymupdf", "extraction_pages": {"pymupdf-text": [1], "unextracted": [2]}}
    _merge_buffered_provenance(meta, {1: "pymupdf-text"}, {})
    assert meta == {"extraction_method": "pymupdf", "extraction_pages": {"pymupdf-text": [1], "unextracted": [2]}}


def _buffer(rungs: list[str | None]) -> MagicMock:
    db = MagicMock()
    db.get_pipeline_state.return_value = {"pages_extracted": len(rungs), "total_pages": None, "extraction_meta": None}
    db.read_pages.return_value = [
        {"page_index": i, "page_text": f"buffered {i}",
         "metadata_json": json.dumps({"page_number": i + 1} | ({"page_method": r} if r else {}))}
        for i, r in enumerate(rungs)
    ]
    return db


def _state(db: MagicMock, *, pages: int, total: int | None = None, method: str | None = None) -> None:
    db.get_pipeline_state.return_value = {
        "pages_extracted": pages, "total_pages": total,
        "extraction_meta": json.dumps({"extraction_method": method}) if method else None,
    }


@pytest.mark.parametrize(
    ("rungs", "total", "method", "wipes"),
    [
        (["mineru", "mineru"], None, None, True),                      # a tripped MinerU run's kept buffer
        (["pymupdf-text", "mineru-no-formula"], None, None, True),     # one MinerU page is enough
        ([None, None], None, None, True),                              # pages that name no rung cannot be proven text-only
        (["mineru", "mineru"], 2, "mineru", True),                     # a finished MinerU extraction is not reused
        (["pymupdf-text", "unextracted"], None, None, False),          # an interrupted pymupdf run is resumed
        (["pymupdf-text", "pymupdf-text"], 2, "pymupdf", False),       # a finished pymupdf extraction is reused
    ],
    ids=["mineru-buffer", "one-mineru-page", "no-rung", "finished-mineru", "own-interrupted", "own-finished"],
)
def test_the_text_only_discard_decision(rungs, total, method, wipes) -> None:
    """nexus-tr1rj.14: wipe when the buffer holds a MinerU rung or a page naming no rung, or a finished
    non-pymupdf extraction; resume a buffer of text-layer rungs and a finished pymupdf extraction."""
    db = _buffer(rungs)
    _state(db, pages=len(rungs), total=total, method=method)

    assert _discard_buffer_for_text_only(db, Path("/a.pdf"), _HASH) is wipes

    assert db.clear_orphan_wal.call_args_list == ([call(_HASH)] if wipes else [])


def test_the_text_only_discard_reads_nothing_for_a_first_run() -> None:
    db = _buffer([])
    _state(db, pages=0)

    assert _discard_buffer_for_text_only(db, Path("/a.pdf"), _HASH) is False

    db.read_pages.assert_not_called()
    db.clear_orphan_wal.assert_not_called()


def test_a_stale_counter_over_an_empty_buffer_wipes_nothing() -> None:
    """The counter claims pages the WAL no longer holds (a wipe an older engine left a counter behind)."""
    db = _buffer([])
    _state(db, pages=3, total=None)

    assert _discard_buffer_for_text_only(db, Path("/a.pdf"), _HASH) is False

    db.clear_orphan_wal.assert_not_called()


def test_extractor_loop_never_wipes_the_buffer_it_runs_beside_the_chunker(stack_absent, tmp_path) -> None:
    """The wipe is not a stage's job (nexus-tr1rj.14, review 2026-10-28): the chunker starts at the same
    time as the extractor and snapshots the pages and the embedded-chunk count, so a wipe inside the
    extractor leaves the chunker with the discarded MinerU text. ``extractor_loop`` resumes what it finds."""
    pdf = _make_pdf(tmp_path / "doc.pdf", [_P1, _P2, _P3])
    db = _buffer(["mineru", "mineru"])

    extractor_loop(pdf, _HASH, db, threading.Event(), extractor="pymupdf")

    db.clear_orphan_wal.assert_not_called()


def test_pipeline_index_pdf_wipes_a_mineru_buffer_before_any_stage_is_submitted(tmp_path) -> None:
    """The ordering the race needs: the wipe is done when the chunker starts, so its snapshot of the
    pages and the embedded-chunk count is the clean one. (The engine-backed twin sees the text.)"""
    from nexus.pipeline_stages import pipeline_index_pdf

    pdf = _make_pdf(tmp_path / "doc.pdf", [_P1, _P2, _P3])
    db = _buffer(["mineru", "mineru"])
    db.create_pipeline.return_value = "resuming"
    db.chunk_counts.return_value = (0, 0)
    seen: dict[str, bool] = {}

    def _chunker(*_a, **_k) -> None:
        seen["wiped_when_the_chunker_started"] = db.clear_orphan_wal.called

    def _extractor(*_a, **_k):
        raise RuntimeError("stop after the stages have started")

    with patch("nexus.pipeline_stages.extractor_loop", _extractor), \
            patch("nexus.pipeline_stages.chunker_loop", _chunker), \
            patch("nexus.pipeline_stages.uploader_loop", return_value=0), \
            patch("nexus.doc_indexer._fence_fail"), \
            pytest.raises(RuntimeError, match="stop after the stages"):
        pipeline_index_pdf(
            pdf, _HASH, "knowledge__t", MagicMock(), db=db, embed_fn=lambda t, m: ([], m),
            extractor="pymupdf", doc_id="1.1.1",
        )

    assert seen == {"wiped_when_the_chunker_started": True}


def test_a_pymupdf_rerun_over_its_own_interrupted_buffer_stays_pymupdf(stack_absent, tmp_path) -> None:
    pdf = _make_pdf(tmp_path / "doc.pdf", [_P1, None, _P3])
    db = _buffer(["pymupdf-text", "unextracted"])

    result = extractor_loop(pdf, _HASH, db, threading.Event(), extractor="pymupdf")

    db.clear_orphan_wal.assert_not_called()
    assert [c.args[1] for c in db.write_page.call_args_list] == [2], "only the page the buffer lacked"
    assert result.metadata["extraction_method"] == "pymupdf"
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 3], "unextracted": [2]}


# ── what `nx store get` calls a degraded page ────────────────────────────────


def _degraded_rows(monkeypatch, meta: dict):
    from types import SimpleNamespace

    from nexus.catalog.store_hook import degraded_pages_for_chunk

    class Reader:
        def resolve_many(self, tumblers):
            return {t: SimpleNamespace(meta=meta) for t in tumblers}

        def close(self) -> None:
            pass

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: Reader())
    return degraded_pages_for_chunk("docs__c", "h" * 64, holders=[("1.1.1", "Doc")])


def test_a_pymupdf_document_lists_only_its_unextracted_pages_as_degraded(monkeypatch) -> None:
    """The text-only extractor's map lists every page under its rung. ``pymupdf-text`` is its normal
    output, so it is not a "Degraded:" line; the pages with no text layer are."""
    rows, held = _degraded_rows(monkeypatch, {
        "extraction_method": "pymupdf", "extraction_pages": {"pymupdf-text": [1, 3], "unextracted": [2]},
    })
    assert held == 1 and rows == [("1.1.1", "Doc", {"unextracted": [2]})]


def test_a_pymupdf_document_with_a_text_layer_on_every_page_has_no_degraded_line(monkeypatch) -> None:
    rows, _held = _degraded_rows(monkeypatch, {
        "extraction_method": "pymupdf", "extraction_pages": {"pymupdf-text": [1, 2, 3]},
    })
    assert rows == []


def test_a_mixture_keeps_its_text_layer_pages_as_degraded(monkeypatch) -> None:
    """MinerU pages plus text-layer pages: here the text layer IS a fall-back, and is listed."""
    rows, _held = _degraded_rows(monkeypatch, {
        "extraction_method": "mineru+degraded", "extraction_pages": {"pymupdf-text": [3, 4]},
    })
    assert rows == [("1.1.1", "Doc", {"pymupdf-text": [3, 4]})]


# ── the library backstop and the repo's setting reader ───────────────────────


class _ReachedTheStore(Exception):
    """Raised by the stand-in for the first store touch of ``index_pdf``."""


def test_index_pdf_with_pymupdf_passes_its_library_backstop_with_the_stack_absent(stack_absent, tmp_path, monkeypatch) -> None:
    """``index_pdf`` refuses a missing stack before it touches the store or the catalog. For ``pymupdf``
    it goes past that point: the first store touch is reached. (The census pins the gate's call shape;
    this pins what the gate does with it.)"""
    from nexus import doc_indexer

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 not empty")
    monkeypatch.setattr(doc_indexer, "_resolve_write_db", MagicMock(side_effect=_ReachedTheStore()))
    kwargs = {"t3": MagicMock(), "collection_name": "docs__c__m__v1", "embed_fn": lambda texts: []}

    with pytest.raises(_ReachedTheStore):
        doc_indexer.index_pdf(pdf, "c", extractor="pymupdf", **kwargs)
    with pytest.raises(PdfStackMissingError):  # control: the same call as any other extractor
        doc_indexer.index_pdf(pdf, "c", extractor="mineru", **kwargs)


def test_a_malformed_pdf_setting_does_not_stop_the_repo_walk_reading_the_extractor(monkeypatch) -> None:
    from structlog.testing import capture_logs

    from nexus.indexer import _repo_pdf_extractor

    monkeypatch.setattr(
        nx_config, "load_config", lambda **kw: {"pdf": {"extractor": "pymupdf", "mineru_page_batch": "many"}},
    )
    with capture_logs() as cap:
        assert _repo_pdf_extractor(Path("/repo")) == "mineru"
    (warning,) = [e for e in cap if e["event"] == "repo_pdf_extractor_config_unreadable"]
    assert "mineru_page_batch" in warning["error"], "the key is named"

    monkeypatch.setattr(nx_config, "load_config", lambda **kw: {"pdf": {"extractor": "pymupdf"}})
    assert _repo_pdf_extractor(Path("/repo")) == "pymupdf"


def test_the_repo_pdf_indexer_hands_its_extractor_to_the_chunker(tmp_path, monkeypatch) -> None:
    from nexus import doc_indexer, indexer

    seen: list[dict] = []
    monkeypatch.setattr(doc_indexer, "_pdf_chunks", lambda *a, **kw: seen.append(kw) or [])
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")

    indexer._index_pdf_file(
        pdf, tmp_path, "docs__c__m__v1", "m", MagicMock(), MagicMock(), "k", {}, "now", 1.0,
        force=True, extractor="pymupdf",
    )
    assert [kw["extractor"] for kw in seen] == ["pymupdf"]
