# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-q6nw0 item 5): which ``extraction_method`` the ladder's rung 3 and the explicit
extractor write.

* A MinerU run with a page on rung 3 or 4 writes ``mineru+degraded`` for the DOCUMENT (the honest
  aggregate) and ``pymupdf-text`` / ``unextracted`` for the PAGE, even when every page is on rung 3.
* The explicit ``--extractor pymupdf`` writes ``pymupdf``.
* ``pymupdf_normalized`` is a legacy value: documents indexed before RDR-237 carry it, nothing writes it.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest

from nexus.pdf_extractor import MineruWorkerError, PDFExtractor

SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"


def _pdf(path: Path, texts: list[str]) -> Path:
    doc = pymupdf.open()
    for text in texts:
        doc.new_page().insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


def _always_fails(pdf_path, start, end, formula_enable=True, **_):
    raise MineruWorkerError("exit 1", returncode=1, stderr_last_line="ImportError: x")


@pytest.mark.parametrize("breaker", [0, 5], ids=["breaker-off", "breaker-above-page-count"])
def test_a_document_whose_every_page_is_on_rung_3_is_mineru_degraded_not_pymupdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, breaker: int,
) -> None:
    monkeypatch.setenv("NEXUS_PDF_PROGRESS_QUIET", "1")
    monkeypatch.delenv("NX_MINERU_FAULT_PAGES", raising=False)
    pdf = _pdf(tmp_path / "doc.pdf", ["alpha one", "bravo two"])
    ext = PDFExtractor()
    pages: list[str] = []
    with patch("nexus.pdf_extractor.do_parse", object()), \
         patch("nexus.config.get_mineru_page_batch", return_value=1), \
         patch("nexus.config.get_pdf_ladder_breaker", return_value=breaker), \
         patch.object(ext, "_mineru_run_isolated", side_effect=_always_fails):
        result = ext._extract_with_mineru(pdf, on_page=lambda i, t, m: pages.append(m["page_method"]))

    assert pages == ["pymupdf-text", "pymupdf-text"], "non-vacuity: both pages really settled on rung 3"
    assert result.metadata["extraction_method"] == "mineru+degraded"
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 2]}


def test_the_explicit_pymupdf_extractor_writes_pymupdf(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "doc.pdf", ["alpha one"])
    result = PDFExtractor()._extract_pymupdf(pdf)
    assert result.metadata["extraction_method"] == "pymupdf"
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1]}


@pytest.mark.lint
def test_nothing_in_src_writes_the_legacy_pymupdf_normalized_value() -> None:
    """A string constant equal to the legacy value would be a write of it (comments and docstrings
    that merely mention it are not equal to it, and the reader sides compare against none)."""
    examined = 0
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        examined += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "pymupdf_normalized":
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert examined > 100, f"non-vacuity: only {examined} source files were scanned"
    assert not offenders, f"the legacy extraction_method value is written at {offenders}"
