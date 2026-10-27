# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.3): ``nx index pdf`` and ``nx dt index`` name the degraded pages at the end of the run.

``index_pdf`` reports what its extraction settled through ``on_extraction`` (a run that extracted and
indexed; never a staleness skip); the two commands collect the reports and print, after their summary,
one line per document with pages below MinerU's first rung. ``index_pdf`` is faked here, so the tests
pin the commands' use of the callback; ``tests/integration/test_rdr237_catalog_extraction.py`` pins
``index_pdf``'s side of it against the engine.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main

pytestmark = pytest.mark.usefixtures("cloud_mode")

DEGRADED = {"mineru-no-formula": [3, 7], "pymupdf-text": [12]}
CLEAN: dict = {}
HEADER = "Degraded pages in {n} document(s)"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _reporting(by_name: dict[str, dict]):
    """A fake ``index_pdf`` that reports ``by_name[path.name]`` through ``on_extraction`` and
    returns what the call shape asks for."""

    def fake(path, **kwargs):
        report = kwargs.get("on_extraction")
        pages = by_name.get(Path(path).name)
        if report is not None and pages is not None:
            report({"extraction_method": "mineru+degraded" if pages else "mineru", "extraction_pages": pages})
        if kwargs.get("return_metadata"):
            return {"chunks": 5, "pages": [1], "title": "", "author": "", "page_count": 1, "pages_with_text": [1]}
        return 5

    return fake


@pytest.fixture
def pdf_dir(tmp_path: Path) -> Path:
    d = tmp_path / "papers"
    d.mkdir()
    for name in ("alpha.pdf", "beta.pdf", "gamma.pdf"):
        (d / name).write_bytes(b"%PDF-dummy")
    return d


# ── nx index pdf --dir ───────────────────────────────────────────────────────


def test_a_directory_run_lists_each_degraded_document_after_the_summary(runner, pdf_dir) -> None:
    fake = _reporting({"alpha.pdf": CLEAN, "beta.pdf": DEGRADED, "gamma.pdf": {"unextracted": [2]}})
    with patch("nexus.doc_indexer.index_pdf", side_effect=fake):
        result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert HEADER.format(n=2) in out
    assert "  beta.pdf: mineru-no-formula 3,7; pymupdf-text 12" in out
    assert "  gamma.pdf: unextracted 2" in out
    assert "alpha.pdf:" not in out.split(HEADER.format(n=2))[1], "a clean document is not listed"
    assert out.index("Summary:") < out.index(HEADER.format(n=2)), "it follows the summary"


def test_a_directory_run_with_nothing_degraded_prints_no_degraded_section(runner, pdf_dir) -> None:
    with patch("nexus.doc_indexer.index_pdf", side_effect=_reporting({"alpha.pdf": CLEAN, "beta.pdf": CLEAN})):
        result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])
    assert result.exit_code == 0, result.output
    assert "Degraded pages" not in result.output


def test_a_failed_document_in_the_batch_is_not_listed_as_degraded(runner, pdf_dir) -> None:
    def fake(path, **kwargs):
        if Path(path).name == "beta.pdf":
            raise RuntimeError("boom")
        return _reporting({"alpha.pdf": DEGRADED})(path, **kwargs)

    with patch("nexus.doc_indexer.index_pdf", side_effect=fake):
        result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])
    assert HEADER.format(n=1) in result.output and "  alpha.pdf:" in result.output
    assert "beta.pdf: mineru" not in result.output


# ── nx index pdf PATH ────────────────────────────────────────────────────────


def _single(runner, tmp_path, pages, *, tty: bool, extra=()):
    pdf = tmp_path / "solo.pdf"
    pdf.write_bytes(b"%PDF-dummy")
    stream = "click.testing._NamedTextIOWrapper.isatty"
    with patch("nexus.doc_indexer.index_pdf", side_effect=_reporting({"solo.pdf": pages})), \
            patch(stream, lambda self: tty):
        return runner.invoke(main, ["index", "pdf", str(pdf), *extra])


@pytest.mark.parametrize("tty", [False, True], ids=["metadata-branch", "plain-branch"])
def test_a_single_file_run_prints_its_degraded_pages_on_both_branches(runner, tmp_path, tty) -> None:
    result = _single(runner, tmp_path, DEGRADED, tty=tty)
    assert result.exit_code == 0, result.output
    assert HEADER.format(n=1) in result.output
    assert "  solo.pdf: mineru-no-formula 3,7; pymupdf-text 12" in result.output


@pytest.mark.parametrize("tty", [False, True], ids=["metadata-branch", "plain-branch"])
def test_a_clean_single_file_run_prints_nothing_extra(runner, tmp_path, tty) -> None:
    result = _single(runner, tmp_path, CLEAN, tty=tty)
    assert result.exit_code == 0, result.output
    assert "Degraded pages" not in result.output


# ── nx dt index ──────────────────────────────────────────────────────────────


def _isolate_dt(monkeypatch, records):
    """Keep the run off DEVONthink and the catalog: only the indexer and the summary are under test."""
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: records)
    monkeypatch.setattr("nexus.commands.dt._stamp_dt_uri_on_entry", lambda *a, **kw: True)
    monkeypatch.setattr("nexus.commands.dt._dt_uri_is_catalogued", lambda uuid: False)
    monkeypatch.setattr("nexus.commands.dt._dt_record_facts", lambda uuid: None)


def _dt(runner, monkeypatch, records, by_name):
    _isolate_dt(monkeypatch, records)
    with patch("nexus.doc_indexer.index_pdf", side_effect=_reporting(by_name)):
        return runner.invoke(main, ["dt", "index", "--uuid", records[0][0]])


def test_a_dt_run_lists_each_degraded_record_with_its_uuid(runner, monkeypatch) -> None:
    result = _dt(
        runner, monkeypatch, [("U1", "/x/a.pdf"), ("U2", "/x/b.pdf")], {"a.pdf": CLEAN, "b.pdf": DEGRADED},
    )
    assert result.exit_code == 0, result.output
    assert HEADER.format(n=1) in result.output
    assert "  b.pdf (U2): mineru-no-formula 3,7; pymupdf-text 12" in result.output
    assert "a.pdf (U1)" not in result.output
    assert result.output.index("Indexed 2 record(s)") < result.output.index(HEADER.format(n=1))


def test_a_dt_run_with_nothing_degraded_prints_no_section(runner, monkeypatch) -> None:
    result = _dt(runner, monkeypatch, [("U1", "/x/a.pdf")], {"a.pdf": CLEAN})
    assert result.exit_code == 0, result.output
    assert "Degraded pages" not in result.output


def test_an_unchanged_dt_record_is_not_listed(runner, monkeypatch) -> None:
    """The staleness skip returns 0 chunks and never reports an extraction."""

    def skipped(path, **kwargs):
        return {"chunks": 0, "pages": [], "title": "", "author": "", "page_count": 0, "pages_with_text": []}

    _isolate_dt(monkeypatch, [("U1", "/x/a.pdf")])
    with patch("nexus.doc_indexer.index_pdf", side_effect=skipped):
        result = runner.invoke(main, ["dt", "index", "--uuid", "U1"])
    assert "Degraded pages" not in result.output


@pytest.mark.parametrize("shape", [(True, 1), (True, 1, None)], ids=["two-tuple", "three-tuple"])
def test_a_dispatcher_returning_the_older_shapes_still_works(runner, monkeypatch, shape) -> None:
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: [("U1", "/x/a.pdf")])
    monkeypatch.setattr("nexus.commands.dt._index_record", lambda *a, **kw: shape)
    result = runner.invoke(main, ["dt", "index", "--uuid", "U1"])
    assert result.exit_code == 0, result.output
    assert "Indexed 1 record(s)" in result.output and "Degraded pages" not in result.output
