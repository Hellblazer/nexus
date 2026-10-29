# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 Phase 1 Step 3 (nexus-tr1rj.5): ``mineru`` is the default extractor; ``auto`` and
``docling`` are aliases for it.

Configs and scripts written before the Docling extractor was removed still say ``auto`` or
``docling``. They keep working and run as ``mineru``, with one line saying so, once per process.
``pymupdf`` (the explicit text-only extractor) passes through. A value that is none of the four
falls back to ``mineru`` with a warning. ``--on-formula-oom`` is retired: accepted, ignored, one
notice, and no longer in the help.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from nexus import config as nexus_config
from nexus.cli import main
from nexus.config import (
    DEFAULT_EXTRACTOR,
    EXTRACTOR_ALIASES,
    EXTRACTOR_CHOICES,
    EXTRACTORS,
    get_pdf_config,
    get_pdf_extractor,
    note_formula_oom_ignored,
    resolve_extractor,
)

_PDF_STUB = {"chunks": 1, "pages": [], "title": "", "author": ""}


@pytest.fixture(autouse=True)
def _fresh_process_notices() -> None:
    """The notices are once per process; each test starts as a new process."""
    nexus_config._NOTICES_SHOWN.clear()
    yield
    nexus_config._NOTICES_SHOWN.clear()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write_config(home: Path, data: dict) -> None:
    path = home / ".config" / "nexus" / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data))


def _notices(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("notice:")]


# ── the vocabulary ──────────────────────────────────────────────────────────


def test_the_default_is_mineru_and_the_choices_are_the_two_extractors_and_the_two_old_names() -> None:
    assert DEFAULT_EXTRACTOR == "mineru"
    assert EXTRACTORS == ("mineru", "pymupdf")
    assert EXTRACTOR_ALIASES == ("auto", "docling")
    assert EXTRACTOR_CHOICES == ("mineru", "pymupdf", "auto", "docling")


# ── resolve_extractor ───────────────────────────────────────────────────────


@pytest.mark.parametrize("value", EXTRACTORS)
def test_a_real_extractor_passes_through_without_a_notice(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert resolve_extractor(value, source="--extractor") == value
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("value", EXTRACTOR_ALIASES)
def test_an_older_name_runs_as_mineru_and_says_so_on_stderr(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert resolve_extractor(value, source="--extractor") == "mineru"
    err = capsys.readouterr().err
    [line] = _notices(err)
    assert f"--extractor says {value!r}" in line and "'mineru'" in line and "'pymupdf' for text only" in line


def test_the_notice_prints_once_per_process_whichever_name_comes_first(capsys: pytest.CaptureFixture[str]) -> None:
    for value in ("auto", "docling", "auto", "docling"):
        assert resolve_extractor(value, source="pdf.extractor in your config") == "mineru"
    assert len(_notices(capsys.readouterr().err)) == 1


def test_a_value_that_is_no_extractor_is_refused() -> None:
    with pytest.raises(ValueError, match="extractor must be one of"):
        resolve_extractor("bogus", source="--extractor")


def test_the_formula_oom_notice_is_once_per_process_and_separate_from_the_alias_notice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    note_formula_oom_ignored()
    note_formula_oom_ignored()
    resolve_extractor("auto", source="--extractor")
    resolve_extractor("auto", source="--extractor")
    lines = _notices(capsys.readouterr().err)
    assert len(lines) == 2
    assert sum("--on-formula-oom is retired and ignored" in line for line in lines) == 1


# ── the config loader ───────────────────────────────────────────────────────


@pytest.mark.parametrize("stored", EXTRACTOR_ALIASES)
def test_a_config_that_says_an_older_name_loads_as_mineru_with_one_notice(
    fake_home: Path, stored: str, capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(fake_home, {"pdf": {"extractor": stored}})
    for _ in range(3):  # the accessors re-read the config on every call
        assert get_pdf_config().extractor == "mineru"
        assert get_pdf_extractor() == "mineru"
    [line] = _notices(capsys.readouterr().err)
    assert f"pdf.extractor in your config says {stored!r}" in line


def test_a_config_that_says_pymupdf_keeps_it_and_prints_nothing(
    fake_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(fake_home, {"pdf": {"extractor": "pymupdf"}})
    assert get_pdf_config().extractor == "pymupdf"
    assert capsys.readouterr().err == ""


def test_no_setting_means_mineru(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert get_pdf_config().extractor == "mineru"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("bad", ["bogus", "MinerU", 7, ""])
def test_an_unknown_config_value_falls_back_to_mineru_with_a_warning_and_no_alias_notice(
    fake_home: Path, bad: object, capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(fake_home, {"pdf": {"extractor": bad}})
    with patch.object(nexus_config, "_log") as log:
        assert get_pdf_config().extractor == "mineru"
    log.warning.assert_called_once_with("invalid pdf.extractor config", value=bad)
    assert _notices(capsys.readouterr().err) == []


def test_the_default_config_template_names_mineru() -> None:
    assert nexus_config._DEFAULTS["pdf"]["extractor"] == "mineru"


# ── nx index pdf ────────────────────────────────────────────────────────────


@pytest.fixture
def pdf_file(fake_home: Path) -> Path:
    path = fake_home / "test.pdf"
    path.write_bytes(b"dummy")
    return path


def _index(args: list[str]) -> tuple[object, dict]:
    with patch("nexus.doc_indexer.index_pdf", return_value=_PDF_STUB) as mock_index:
        result = CliRunner().invoke(main, ["index", "pdf", *args])
    assert result.exit_code == 0, result.output
    return result, mock_index.call_args.kwargs


def test_index_pdf_with_no_choice_runs_mineru(pdf_file: Path) -> None:
    result, kwargs = _index([str(pdf_file)])
    assert kwargs["extractor"] == "mineru"
    assert _notices(result.output) == []


@pytest.mark.parametrize("name", EXTRACTOR_ALIASES)
def test_index_pdf_flag_with_an_older_name_runs_mineru_with_one_notice(pdf_file: Path, name: str) -> None:
    result, kwargs = _index([str(pdf_file), "--extractor", name])
    assert kwargs["extractor"] == "mineru"
    [line] = _notices(result.output)
    assert f"--extractor says {name!r}" in line


@pytest.mark.parametrize("name", EXTRACTOR_ALIASES)
def test_index_pdf_config_with_an_older_name_runs_mineru_with_one_notice(
    fake_home: Path, pdf_file: Path, name: str,
) -> None:
    _write_config(fake_home, {"pdf": {"extractor": name}})
    result, kwargs = _index([str(pdf_file)])
    assert kwargs["extractor"] == "mineru"
    assert len(_notices(result.output)) == 1


def test_index_pdf_pymupdf_passes_through_without_a_notice(pdf_file: Path) -> None:
    result, kwargs = _index([str(pdf_file), "--extractor", "pymupdf"])
    assert kwargs["extractor"] == "pymupdf"
    assert _notices(result.output) == []


def test_index_pdf_still_refuses_an_unknown_extractor(pdf_file: Path) -> None:
    result = CliRunner().invoke(main, ["index", "pdf", str(pdf_file), "--extractor", "magic"])
    assert result.exit_code != 0


@pytest.mark.parametrize("value", ["fail", "docling"])
def test_on_formula_oom_is_accepted_ignored_and_noticed_once(pdf_file: Path, value: str) -> None:
    result, kwargs = _index([str(pdf_file), "--on-formula-oom", value])
    assert "on_formula_oom" not in kwargs, "the retired flag reaches no library function"
    [line] = _notices(result.output)
    assert "--on-formula-oom is retired and ignored" in line


def test_on_formula_oom_is_not_in_the_help_and_the_extractor_help_is_current() -> None:
    result = CliRunner().invoke(main, ["index", "pdf", "--help"])
    assert result.exit_code == 0
    assert "--on-formula-oom" not in result.output
    assert "'mineru'" in result.output and "pymupdf" in result.output
    assert "formula-stripped" not in result.output


# ── nx dt index and nx dt capture ───────────────────────────────────────────


def test_the_dt_verbs_take_the_pdf_extractor_setting_and_still_accept_every_choice() -> None:
    """nexus-tr1rj.14: no flag means ``pdf.extractor`` (``mineru`` when it is unset), as for
    ``nx index pdf``; the flag's own default is therefore None."""
    from nexus.commands import dt as dt_module

    for verb in ("index", "capture"):
        option = next(p for p in dt_module.dt.commands[verb].params if p.name == "extractor")
        assert option.default is None, verb
        assert tuple(option.type.choices) == EXTRACTOR_CHOICES, verb


# ── the title chain no longer starts at the removed extractor's key ─────────


def test_the_title_chain_ignores_the_removed_extractors_title_key() -> None:
    """RDR-237: the chain is the XMP ``pdf_title``, then the first heading, then the file name.
    Metadata written by an older nexus, or a fake that still carries ``docling_title``, must not
    win over them."""
    from nexus.indexer_utils import resolve_pdf_title

    path = Path("/data/2512.11001.pdf")
    body = "# The Heading Title\n\nBody text.\n"
    assert resolve_pdf_title({"docling_title": "Stale Title"}, path, body) == "The Heading Title"
    assert resolve_pdf_title({"docling_title": "Stale Title", "pdf_title": "XMP Title"}, path, body) == "XMP Title"
    assert resolve_pdf_title({"docling_title": "Stale Title"}, path, "plain text") == "2512.11001"


def test_no_extraction_path_writes_the_removed_title_key(tmp_path: Path) -> None:
    from nexus.pdf_extractor import PDFExtractor

    import pymupdf

    pdf = tmp_path / "t.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "Some page text that is long enough to extract.", fontsize=12)
    doc.save(str(pdf))
    doc.close()
    assert "docling_title" not in PDFExtractor().extract(pdf, extractor="pymupdf").metadata


# ── the library entry points ────────────────────────────────────────────────


def test_extract_resolves_an_older_name_with_the_notice(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A library caller (an MCP tool, a script) that still passes ``auto`` gets MinerU and one notice."""
    from nexus.pdf_extractor import ExtractionResult, PDFExtractor

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    extractor = PDFExtractor()
    fake = ExtractionResult(text="Plain text, long enough. " * 8, metadata={"extraction_method": "mineru"})
    with patch.object(extractor, "_extract_with_mineru", return_value=fake) as mineru, \
            patch("nexus.pdf_stack.require"):
        for _ in range(2):
            extractor.extract(pdf, extractor="auto", allow_degraded=True)
    assert mineru.call_count == 2
    assert len(_notices(capsys.readouterr().err)) == 1


@pytest.mark.parametrize("module_name, qualname", [
    ("nexus.doc_indexer", "index_pdf"),
    ("nexus.doc_indexer", "batch_index_pdfs"),
    ("nexus.doc_indexer", "_pdf_chunks"),
    ("nexus.pdf_extractor", "PDFExtractor.extract"),
    ("nexus.pipeline_stages", "pipeline_index_pdf"),
    ("nexus.pipeline_stages", "extractor_loop"),
    ("nexus.commands.dt", "_index_record"),
    ("nexus.indexer", "_index_pdf_file"),
])
def test_every_library_entry_point_defaults_to_mineru(module_name: str, qualname: str) -> None:
    import importlib
    import inspect

    obj = importlib.import_module(module_name)
    for name in qualname.split("."):
        obj = getattr(obj, name)
    default = inspect.signature(obj).parameters["extractor"].default
    assert default == "mineru", f"{module_name}.{qualname} defaults to {default!r}"
