# SPDX-License-Identifier: AGPL-3.0-or-later
"""resolve_pdf_title rejects producer placeholders in the PDF's metadata title (nexus-tr1rj.22).

Both PDF paths read the metadata title with PyMuPDF and the chain ranks it
first, so a producer artifact ("Microsoft Word - x.docx", "untitled", a bare file name) used to beat
a good first heading. A placeholder falls through to the first H1, then the file name, the way a
section-heading H1 does.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.indexer_utils import _pdf_title_is_placeholder, resolve_pdf_title

PDF = Path("/data/2512.11001.pdf")
BODY = "# Rethinking Query Optimization for Multi-Agent Systems\n\nBody text.\n"
H1 = "Rethinking Query Optimization for Multi-Agent Systems"

REJECTED = [
    "",
    "   ",
    "-",
    "--",
    "_",
    "...",
    "untitled",
    "Untitled",
    "UNTITLED",
    "Untitled document",
    "Untitled Presentation",
    "Untitled-1",
    "Untitled 2",
    "(untitled)",
    "Document1",
    "Document 12",
    "Slide 1",
    "Slide1",
    "PowerPoint Presentation",
    "Microsoft Word Document",
    "title",
    "x.pdf",
    "paper.docx",
    "My Draft v2.doc",
    "slides.PPTX",
    "main.tex",
    "C:\\Users\\sam\\Desktop\\thesis.docx",
    "/home/sam/report.pdf",
    "Microsoft Word - x.docx",
    "Microsoft Word - Annual Report",
    "Microsoft PowerPoint - deck.pptx",
    "Microsoft Excel - budget.xlsx",
    "Microsoft Word - ",
    "2512.11001",  # the PDF's own file stem
    "2512.11001.pdf",
]

ACCEPTED = [
    "XMP Title",
    "The Declared Title",
    "Attention Is All You Need",
    "Rethinking Query Optimization for Multi-Agent Systems [Vision]",
    "Untitled Works of the Early Baroque",  # begins with a placeholder word, is a title
    "Document Understanding with Layout Transformers",
    "Slide Rules and the History of Computation",
    "On the pdf format",  # mentions an extension word with no dot
    "Microsoft and the Antitrust Case",  # a company name is not a producer prefix
    "Rethinking .NET Garbage Collection",
]


@pytest.mark.parametrize("title", REJECTED)
def test_a_placeholder_metadata_title_is_rejected(title: str) -> None:
    assert _pdf_title_is_placeholder(title, PDF), repr(title)


@pytest.mark.parametrize("title", ACCEPTED)
def test_a_real_metadata_title_is_accepted(title: str) -> None:
    assert not _pdf_title_is_placeholder(title, PDF), repr(title)


@pytest.mark.parametrize("title", REJECTED)
def test_a_placeholder_never_beats_the_first_heading(title: str) -> None:
    assert resolve_pdf_title({"pdf_title": title}, PDF, BODY) == H1


@pytest.mark.parametrize("title", REJECTED)
def test_a_placeholder_with_no_heading_falls_to_the_file_name(title: str) -> None:
    assert resolve_pdf_title({"pdf_title": title}, PDF, "plain text only") == "2512.11001"


@pytest.mark.parametrize("title", ACCEPTED)
def test_a_real_metadata_title_still_beats_the_first_heading(title: str) -> None:
    assert resolve_pdf_title({"pdf_title": title}, PDF, BODY) == title


def test_a_placeholder_then_a_section_heading_h1_falls_to_the_file_name() -> None:
    assert resolve_pdf_title({"pdf_title": "untitled"}, PDF, "# Abstract\n\nWe study.\n") == "2512.11001"
