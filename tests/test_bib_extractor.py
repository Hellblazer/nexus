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


class TestDoiLabelPreference:
    """nexus-liir: when a document contains both a labeled paper-DOI
    AND bare reference-DOIs, the labeled form wins. This eliminates
    the wrong-paper class where Docling's chunk order placed the
    references section before the page-1 banner."""

    def test_labeled_doi_wins_over_earlier_bare_doi(self) -> None:
        from nexus.bib_extractor import extract_doi

        # Reference list comes first (bare DOIs); paper banner with
        # 'DOI:' label appears later. Pre-fix: returned the first
        # bare DOI (a citation). Post-fix: returns the labeled one.
        text = (
            "References\n[1] 10.1145/PBFT.OLD\n[2] 10.1109/CITED.OTHER\n"
            "----\n"
            "DOI: 10.1145/THIS-PAPER\nAbstract: ..."
        )
        assert extract_doi(text) == "10.1145/THIS-PAPER"

    def test_doi_org_url_label_wins(self) -> None:
        from nexus.bib_extractor import extract_doi

        text = (
            "[3] cites 10.1145/CITATION\n"
            "Available at https://doi.org/10.4230/LIPIcs.OPODIS.2015.7"
        )
        assert extract_doi(text) == "10.4230/LIPIcs.OPODIS.2015.7"

    def test_dx_doi_org_url_label_wins(self) -> None:
        """Older papers use the dx.doi.org domain. Same preference."""
        from nexus.bib_extractor import extract_doi

        text = "ref 10.1/old\nhttp://dx.doi.org/10.1145/NEW.PAPER"
        assert extract_doi(text) == "10.1145/NEW.PAPER"

    def test_falls_back_to_bare_when_no_label(self) -> None:
        """No labeled DOI in text: keep the original behavior of
        returning the first bare DOI. Covers older papers that don't
        print 'DOI:' explicitly on page 1."""
        from nexus.bib_extractor import extract_doi

        text = "Authors: A, B, C. 10.1145/BARE.DOI\n\nAbstract..."
        assert extract_doi(text) == "10.1145/BARE.DOI"

    def test_first_labeled_doi_wins_when_multiple(self) -> None:
        """Some papers print the DOI twice (header + footer). Take
        the first labeled occurrence."""
        from nexus.bib_extractor import extract_doi

        text = (
            "Header DOI: 10.1145/FIRST\n"
            "...content...\n"
            "Footer doi: 10.1145/SECOND"
        )
        assert extract_doi(text) == "10.1145/FIRST"

    def test_preserves_strip_trailing_punct_with_label(self) -> None:
        from nexus.bib_extractor import extract_doi

        text = "DOI: 10.1145/X.Y.Z, ..."
        assert extract_doi(text) == "10.1145/X.Y.Z"


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


# ── Reference-section bound + placeholder rejection (nexus-kho0p) ────────────


class TestDoiReferenceSectionBound:
    """nexus-kho0p: the labeled-DOI preference (nexus-liir) was not bounded
    to the region above the bibliography. nexus-liir's stated justification
    — "reference-section DOIs usually appear bare in numbered citation
    lists" (bib_extractor.py:38-46) — is falsified by preprints whose
    references carry explicit ``doi:`` labels. On such a paper, with no DOI
    of its own in the header, the labeled search reached into the
    bibliography and returned a CITED paper's DOI.

    Measured case: Meta-Harness (arXiv 2603.28052) yielded 10.1145/3591300,
    its own reference [8] (LMQL / "Prompting Is Programming", PLDI 2023).
    """

    def test_labeled_doi_in_references_is_not_returned(self) -> None:
        """The nexus-kho0p defect, reduced. No header DOI; the only labeled
        DOI in the document is a bibliography entry. Returning None is
        correct — the caller falls back to the arXiv ID, which is
        authoritative for a preprint."""
        from nexus.bib_extractor import extract_doi

        text = (
            "Meta-Harness: End-to-End Optimization of Model Harnesses\n"
            "Yoonho Lee, Roshen Nair, Chelsea Finn\n"
            "Abstract. We introduce Meta-Harness, an outer-loop system.\n"
            "References\n"
            "[8] Luca Beurer-Kellner et al. Prompting Is Programming. "
            "doi:10.1145/3591300\n"
        )
        assert extract_doi(text) is None

    def test_header_doi_still_wins_over_labeled_references(self) -> None:
        """The nexus-liir class must not regress: a paper that DOES print
        its own DOI still gets it, even when the references also carry
        labeled DOIs."""
        from nexus.bib_extractor import extract_doi

        text = (
            "Credo: Declarative Control of LLM Pipelines\n"
            "doi:10.14778/3827998.3828054\n"
            "References\n"
            "[6] Meta-Harness. doi:10.1145/3591300\n"
        )
        assert extract_doi(text) == "10.14778/3827998.3828054"

    def test_bare_doi_above_references_still_found(self) -> None:
        """The bare-DOI fallback keeps working inside the bounded region."""
        from nexus.bib_extractor import extract_doi

        text = "Some Paper\n10.1038/s41586-021-04096-9\nReferences\n[1] doi:10.1145/999"
        assert extract_doi(text) == "10.1038/s41586-021-04096-9"

    @pytest.mark.parametrize(
        "heading",
        ["References", "REFERENCES", "## References", "Bibliography",
         "BIBLIOGRAPHY", "## Bibliography", "REFERENCES CITED"],
    )
    def test_heading_variants_bound_the_search(self, heading: str) -> None:
        from nexus.bib_extractor import extract_doi

        text = f"Paper Title\nAbstract here.\n{heading}\n[1] doi:10.1145/3591300\n"
        assert extract_doi(text) is None

    def test_nothing_below_the_bibliography_is_harvested(self) -> None:
        """In a normally-ordered document, everything below the references
        heading is a citation or an appendix — neither is the paper's own
        DOI. Both are refused, including the appendix line, which carries no
        citation marker and so is caught by position rather than by
        _CITATION_LINE_RE."""
        from nexus.bib_extractor import extract_doi

        text = (
            "Paper Title\nAbstract.\n"
            "References\n[1] doi:10.1145/3591300\n"
            "A Appendix\nFurther detail. doi:10.1109/OTHER.2024\n"
        )
        assert extract_doi(text) is None

    def test_citation_only_document_yields_none(self) -> None:
        """When every DOI in the document sits in a bibliography entry, the
        answer is None — the paper printed no DOI of its own. This is the
        nexus-kho0p case reduced to its essential shape."""
        from nexus.bib_extractor import extract_doi

        text = (
            "Paper Title\nAbstract.\n"
            "References\n"
            "[1] Someone et al. doi:10.1145/3591300\n"
            "[2] Another. doi:10.1109/OTHER.2024\n"
        )
        assert extract_doi(text) is None

    def test_references_appearing_twice_bounds_at_the_first(self) -> None:
        from nexus.bib_extractor import extract_doi

        text = (
            "Paper Title\ndoi:10.14778/REAL.DOI\n"
            "References\n[1] doi:10.1145/3591300\n"
            "Appendix A\nReferences\n[1] doi:10.1109/OTHER.2024\n"
        )
        assert extract_doi(text) == "10.14778/REAL.DOI"

    def test_no_heading_falls_back_to_whole_text(self) -> None:
        """When no bibliography heading is present there is nothing to bound
        against; behaviour is unchanged from before nexus-kho0p."""
        from nexus.bib_extractor import extract_doi

        assert extract_doi("Paper. doi:10.1145/3654657.3654729") == "10.1145/3654657.3654729"


class TestDoiWrappedCitationEntry:
    """nexus-kho0p regression, taken verbatim from arXiv 2603.28052.

    Invented fixtures put the DOI on the same line as the ``[N]`` marker,
    which line-level citation detection catches. Real reference entries wrap:
    the DOI lands on a continuation line with no marker and no "et al.", so
    only the position guard sees it. This fixture is the measured text — it
    is the case that passed a fixture-only test run and still returned the
    wrong DOI against the actual PDF.
    """

    REAL = (
        "Meta-Harness: End-to-End Optimization of Model Harnesses\n"
        "Yoonho Lee, Roshen Nair, Qizheng Zhang, Kangwook Lee, Omar Khattab, Chelsea Finn\n"
        "Abstract. The performance of large language model systems depends not\n"
        "only on model weights, but also on their harness.\n"
        "arXiv:2603.28052\n"
        "References\n"
        "[8] Luca Beurer-Kellner, Marc Fischer, and Martin Vechev. Prompting is programming:\n"
        "A query language for large language models.Proceedings of the ACM on Programming\n"
        "Languages, 7(PLDI):1946\u20131969, June 2023. ISSN 2475-1421. doi: 10.1145/3591300. URL\n"
    )

    def test_wrapped_citation_doi_is_not_returned(self) -> None:
        from nexus.bib_extractor import extract_doi

        assert extract_doi(self.REAL) is None

    def test_caller_still_gets_a_correct_identifier(self) -> None:
        """The point of returning None: the arXiv ID is right there."""
        from nexus.bib_extractor import extract_identifiers

        ids = extract_identifiers(body_text=self.REAL)
        assert ids["doi"] is None
        assert ids["arxiv"] == "2603.28052"


class TestDoiPlaceholderRejection:
    """nexus-kho0p: unfilled LaTeX template DOIs are not identifiers. The
    acmart default ``10.1145/nnnnnnn.nnnnnnn`` was observed verbatim on FOUR
    different papers in the user's corpus."""

    @pytest.mark.parametrize(
        "placeholder",
        [
            "doi:10.1145/nnnnnnn.nnnnnnn",       # acmart, unfilled
            "doi:10.4230/LIPIcs.SoCG.2026.XX",   # LIPIcs, unfilled
            "doi:10.1109/XXX.0000.0000000",      # IEEE template
            "doi:10.1364/ao.XX.XXXXXX",          # Optica template
        ],
    )
    def test_placeholder_dois_rejected(self, placeholder: str) -> None:
        from nexus.bib_extractor import extract_doi

        assert extract_doi(placeholder) is None

    def test_placeholder_shaped_but_real_suffixes_survive(self) -> None:
        """Guard against over-rejection. These are the existing fixtures in
        this file plus real-world forms; a general 'repeated character run'
        rule would wrongly kill them."""
        from nexus.bib_extractor import extract_doi

        for doi in (
            "10.1145/AAAA.BBBB",
            "10.1109/CCCC.DDDD",
            "10.1109/X.Y",
            "10.48550/arXiv.2503.07641",   # 'arXiv' contains X
            "10.1109/ABC.2024.012345",
            "10.1038/s41586-021-04096-9",
        ):
            assert extract_doi(f"doi:{doi}") == doi

    def test_url_suffix_not_absorbed(self) -> None:
        """Observed: 10.3389/fpsyg.2014.01053/pdf — the regex swallowed a
        URL path segment."""
        from nexus.bib_extractor import extract_doi

        got = extract_doi("https://doi.org/10.3389/fpsyg.2014.01053/pdf")
        assert got == "10.3389/fpsyg.2014.01053"

    def test_percent_encoded_and_file_extensions_rejected(self) -> None:
        from nexus.bib_extractor import extract_doi

        assert extract_doi("doi:10.1234/some%20file%20name.pdf") is None
        assert extract_doi("doi:10.1234/paper.pdf") is None

    def test_unbalanced_open_paren_trimmed(self) -> None:
        """Observed: 10.1016/S0004-3702(01 — truncated mid-parenthesis."""
        from nexus.bib_extractor import extract_doi

        assert extract_doi("doi:10.1016/S0004-3702(01") == "10.1016/S0004-3702"

    def test_balanced_parens_preserved(self) -> None:
        from nexus.bib_extractor import extract_doi

        assert extract_doi("doi:10.1016/S0004-3702(01)00108-4") == "10.1016/S0004-3702(01)00108-4"


class TestPreprintIdentifierFallback:
    """nexus-kho0p end-to-end: the whole point of returning None rather than
    a reference DOI is that the arXiv ID is still there and is correct."""

    def test_meta_harness_shape_falls_back_to_arxiv(self) -> None:
        from nexus.bib_extractor import extract_identifiers

        ids = extract_identifiers(
            filename="Meta-Harness- End-to-End Optimization of Model Harnesses.pdf",
            body_text=(
                "Meta-Harness: End-to-End Optimization of Model Harnesses\n"
                "arXiv:2603.28052\n"
                "References\n"
                "[8] Prompting Is Programming. doi:10.1145/3591300\n"
            ),
        )
        assert ids["doi"] is None
        assert ids["arxiv"] == "2603.28052"
