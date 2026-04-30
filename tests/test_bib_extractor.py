# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for nexus.bib_extractor (nexus-sbzr).

DOI + arXiv ID regex extractors. These run against PDF body text to
find the paper's canonical identifier before falling back to fuzzy
title search at OpenAlex/Semantic Scholar. Filename slugs (e.g.
``mfaz.pdf``) are too short to disambiguate via title-search; a DOI
or arXiv ID guarantees the right paper.

All extraction is text-only — no network, no PDF re-parsing.
"""
from __future__ import annotations

import pytest


# ── DOI extraction ──────────────────────────────────────────────────────────


class TestExtractDoi:
    def test_extracts_acm_doi(self) -> None:
        from nexus.bib_extractor import extract_doi

        text = "Published at SIGMOD 2024. DOI: 10.1145/3654657.3654729"
        assert extract_doi(text) == "10.1145/3654657.3654729"

    def test_extracts_ieee_doi(self) -> None:
        from nexus.bib_extractor import extract_doi

        text = "doi:10.1109/ICDE.2024.00123\nAuthors: ..."
        assert extract_doi(text) == "10.1109/ICDE.2024.00123"

    def test_extracts_nature_doi(self) -> None:
        from nexus.bib_extractor import extract_doi

        text = "https://doi.org/10.1038/s41586-021-04096-9"
        assert extract_doi(text) == "10.1038/s41586-021-04096-9"

    def test_extracts_first_doi_when_multiple(self) -> None:
        """Papers often cite other DOIs in their references. The
        canonical paper-DOI is on page 1 (header / footer / abstract)
        and we want THAT one, not a citation. Take the first match."""
        from nexus.bib_extractor import extract_doi

        text = (
            "Title of Paper\nDOI: 10.1145/AAAA.BBBB\n"
            "References:\n[1] 10.1109/CCCC.DDDD\n[2] 10.1038/EEEE.FFFF"
        )
        assert extract_doi(text) == "10.1145/AAAA.BBBB"

    def test_handles_arxiv_doi_form(self) -> None:
        from nexus.bib_extractor import extract_doi

        text = "arXiv:2503.07641. doi: 10.48550/arXiv.2503.07641"
        assert extract_doi(text) == "10.48550/arXiv.2503.07641"

    def test_strips_trailing_punctuation(self) -> None:
        """DOIs in body text often end with comma/period/parenthesis
        (e.g. 'see [42] (10.1145/X.Y).'). Strip those — they aren't
        part of the DOI."""
        from nexus.bib_extractor import extract_doi

        for trailing in (".", ",", ");", ")", ";", "]", ":"):
            assert extract_doi(f"see DOI 10.1145/A.B{trailing}") == "10.1145/A.B"

    def test_returns_none_when_no_doi(self) -> None:
        from nexus.bib_extractor import extract_doi

        assert extract_doi("Just a paper with no DOI listed.") is None
        assert extract_doi("") is None
        assert extract_doi("10.123/x") is None  # too few digits in registrant
        assert extract_doi("not.a.doi/at-all") is None

    def test_case_insensitive(self) -> None:
        from nexus.bib_extractor import extract_doi

        text = "DOI: 10.1109/ABC.2024.012345"
        assert extract_doi(text) == "10.1109/ABC.2024.012345"


# ── arXiv ID extraction ─────────────────────────────────────────────────────


class TestExtractArxivId:
    def test_extracts_new_style_id(self) -> None:
        """New-style arXiv IDs (post-April-2007): YYMM.NNNNN(vN)?
        4-digit YYMM + 4-or-5-digit serial."""
        from nexus.bib_extractor import extract_arxiv_id

        assert extract_arxiv_id("Submitted as arXiv:2503.07641") == "2503.07641"
        assert extract_arxiv_id("see 1706.03762v5 for details") == "1706.03762"

    def test_extracts_from_filename(self) -> None:
        """ArXiv often distributes papers as <id>.pdf, so the
        filename alone identifies them."""
        from nexus.bib_extractor import extract_arxiv_id

        assert extract_arxiv_id("/papers/2503.07641.pdf") == "2503.07641"
        assert extract_arxiv_id("deep-artmap-2503.07641.pdf") == "2503.07641"

    def test_returns_none_when_absent(self) -> None:
        from nexus.bib_extractor import extract_arxiv_id

        assert extract_arxiv_id("just text with no arxiv id") is None
        assert extract_arxiv_id("") is None
        assert extract_arxiv_id("see paper 12.345") is None  # too few digits
        assert extract_arxiv_id("see paper 12345.6789") is None  # too many YYMM digits

    def test_does_not_match_on_random_8_digit_strings(self) -> None:
        """Year+page-number patterns like '2024.34567' shouldn't match
        unless preceded by 'arXiv:' or appearing as a filename."""
        from nexus.bib_extractor import extract_arxiv_id

        # A standalone 4-digit.5-digit pattern is ambiguous; require
        # the arXiv: prefix or filename context for safety.
        assert extract_arxiv_id("Page 2024.34567 of the chapter") is None

    def test_strips_version_suffix(self) -> None:
        from nexus.bib_extractor import extract_arxiv_id

        assert extract_arxiv_id("arXiv:1706.03762v5") == "1706.03762"
        assert extract_arxiv_id("arXiv:1706.03762v15") == "1706.03762"


# ── Combined extractor ──────────────────────────────────────────────────────


class TestExtractIdentifiers:
    """The combined entry point picks the best identifier available
    for a (filename, body_text) pair. Order: DOI > arXiv ID > None.
    DOI is more authoritative because it can resolve arXiv preprints
    AND non-arXiv venues; arXiv IDs only work for arXiv papers."""

    def test_prefers_doi_over_arxiv(self) -> None:
        from nexus.bib_extractor import extract_identifiers

        ids = extract_identifiers(
            filename="2503.07641.pdf",
            body_text="DOI: 10.48550/arXiv.2503.07641\narXiv:2503.07641",
        )
        assert ids["doi"] == "10.48550/arXiv.2503.07641"
        assert ids["arxiv"] == "2503.07641"

    def test_arxiv_only(self) -> None:
        from nexus.bib_extractor import extract_identifiers

        ids = extract_identifiers(
            filename="paper.pdf",
            body_text="See arXiv:1706.03762 for the original.",
        )
        assert ids["doi"] is None
        assert ids["arxiv"] == "1706.03762"

    def test_doi_only(self) -> None:
        from nexus.bib_extractor import extract_identifiers

        ids = extract_identifiers(
            filename="paper.pdf",
            body_text="DOI: 10.1109/X.Y",
        )
        assert ids["doi"] == "10.1109/X.Y"
        assert ids["arxiv"] is None

    def test_both_none(self) -> None:
        from nexus.bib_extractor import extract_identifiers

        ids = extract_identifiers(filename="paper.pdf", body_text="just text")
        assert ids == {"doi": None, "arxiv": None}
