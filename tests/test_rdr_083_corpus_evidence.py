# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for RDR-083 Corpus-Evidence Tokens.

Four surfaces under test:

  1. ``chash:`` span scanner — detects ``[text](chash:<hash>)`` markdown
     links and prose citations. Respects fenced code blocks.
  2. ``AnchorResolver`` — ``{{nx-anchor:<collection>[|top=N]}}`` reads
     top-N topics for a collection from ``topic_assignments``. Registers
     into RDR-082's ResolverRegistry — this test protects the
     extension-point invariant end-to-end.
  3. ``check-grounding`` logic — coverage ratio (chash / total).
  4. ``check-extensions`` logic — threshold query against projection
     data.

v1 scope intentionally does NOT verify that each ``chash:`` hash
resolves to an indexed chunk — that requires cross-collection T3
lookups and is deferred to a follow-up.  We verify shape + counting
only; what ships is actionable metrics, not dead-letter validation.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ── chash: span scanner ──────────────────────────────────────────────────────


class TestCitationScanner:

    def test_detects_chash_markdown_link(self) -> None:
        from nexus.doc.citations import scan_citations

        md = "Per [boundary feedback](chash:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef), the model..."
        cites = scan_citations(md)
        chashes = [c for c in cites if c.kind == "chash"]
        assert len(chashes) == 1
        assert chashes[0].chash == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        assert "boundary feedback" in chashes[0].display

    def test_detects_prose_author_year_citation(self) -> None:
        from nexus.doc.citations import scan_citations

        md = "As shown by [Grossberg 2013], attention gates the feedback loop."
        cites = scan_citations(md)
        prose = [c for c in cites if c.kind == "prose"]
        assert len(prose) == 1
        assert "Grossberg" in prose[0].display

    def test_detects_bracketed_number_citation(self) -> None:
        from nexus.doc.citations import scan_citations

        md = "See [12] for the derivation."
        cites = scan_citations(md)
        brackets = [c for c in cites if c.kind == "bracket"]
        assert len(brackets) == 1

    def test_ignores_citation_inside_fenced_code(self) -> None:
        from nexus.doc.citations import scan_citations

        md = (
            "Real: [Grossberg 2013]\n"
            "```\n"
            "Example only: [Fake 2020]\n"
            "[12] bracketed in fence\n"
            "```\n"
        )
        cites = scan_citations(md)
        # Only the real prose citation survives
        assert len(cites) == 1
        assert "Grossberg" in cites[0].display

    def test_invalid_chash_length_skipped(self) -> None:
        """A chash URL with a too-short hash is not counted."""
        from nexus.doc.citations import scan_citations

        md = "See [ref](chash:tooshort) in the appendix."
        cites = scan_citations(md)
        assert [c for c in cites if c.kind == "chash"] == []

    @pytest.mark.parametrize("token", [
        "[Error 2013]",
        "[RFC 2119]",
        "[Note 2024]",
        "[Figure 2020]",
        "[Table 2023]",
        "[Closes 2025]",
        "[Fixes 2024]",
        "[Issue 2022]",
        "[Draft 2025]",
        "[Release 2024]",
        "[Section 2020]",
        "[Warning 2023]",
    ])
    def test_prose_stoplist_suppresses_false_positives(self, token: str) -> None:
        """Dev-prose false-positives must not inflate the prose count."""
        from nexus.doc.citations import scan_citations

        cites = scan_citations(f"See {token} for the derivation.")
        assert [c for c in cites if c.kind == "prose"] == [], (
            f"{token} should have been suppressed by the stop-list"
        )

    def test_real_author_citations_still_detected_after_stoplist(self) -> None:
        """Stop-list must not regress the positive case."""
        from nexus.doc.citations import scan_citations

        md = (
            "Per [Grossberg 2013] and [Ashby 1956], the loop is stable. "
            "Not [Note 2024] though."
        )
        prose = [c for c in scan_citations(md) if c.kind == "prose"]
        displays = sorted(c.display for c in prose)
        assert displays == ["Ashby 1956", "Grossberg 2013"]


# ── AnchorResolver — RDR-082 extension point ─────────────────────────────────


class TestAnchorResolver:

    def test_top_n_topics_rendered_as_list(self) -> None:
        from nexus.doc.resolvers_corpus import AnchorResolver

        fake_tax = MagicMock()
        fake_tax.top_topics_for_collection = MagicMock(return_value=[
            {"label": "Pattern Matching", "chunks": 120},
            {"label": "Vector Search", "chunks": 80},
            {"label": "Catalog Registry", "chunks": 60},
        ])

        r = AnchorResolver(taxonomy=fake_tax)
        out = r.resolve("docs__art", field=None, filters={"top": "3"})
        assert "Pattern Matching" in out
        assert "Vector Search" in out
        assert "Catalog Registry" in out
        fake_tax.top_topics_for_collection.assert_called_once_with(
            "docs__art", top_n=3,
        )

    def test_default_top_is_5_when_filter_omitted(self) -> None:
        from nexus.doc.resolvers_corpus import AnchorResolver

        fake_tax = MagicMock()
        fake_tax.top_topics_for_collection = MagicMock(return_value=[
            {"label": "x", "chunks": 1},
        ])
        r = AnchorResolver(taxonomy=fake_tax)
        r.resolve("docs__x", field=None, filters={})
        fake_tax.top_topics_for_collection.assert_called_once_with(
            "docs__x", top_n=5,
        )

    def test_empty_projection_raises(self) -> None:
        """A collection with no projection data must not silently
        render an empty list — that's not evidence, it's absence."""
        from nexus.doc.resolvers import ResolutionError
        from nexus.doc.resolvers_corpus import AnchorResolver

        fake_tax = MagicMock()
        fake_tax.top_topics_for_collection = MagicMock(return_value=[])
        r = AnchorResolver(taxonomy=fake_tax)
        with pytest.raises(ResolutionError):
            r.resolve("docs__no-projection", field=None, filters={"top": "5"})

    def test_registers_into_082_registry_end_to_end(self) -> None:
        """The AnchorResolver plugs into RDR-082's ResolverRegistry with
        no changes to parser/engine/CLI — this is the invariant."""
        from nexus.doc.render import render_text
        from nexus.doc.resolvers import ResolverRegistry
        from nexus.doc.resolvers_corpus import AnchorResolver

        fake_tax = MagicMock()
        fake_tax.top_topics_for_collection = MagicMock(return_value=[
            {"label": "Boundary Feedback", "chunks": 50},
            {"label": "Top-Down Expectation", "chunks": 42},
        ])
        reg = ResolverRegistry({})
        reg.register("nx-anchor", AnchorResolver(taxonomy=fake_tax))

        md = "Corpus shape: {{nx-anchor:docs__art|top=2}}"
        out, resolved, _ = render_text(md, reg)
        assert "Boundary Feedback" in out and "Top-Down Expectation" in out
        assert resolved == 1


# ── check-grounding logic ────────────────────────────────────────────────────


class TestGroundingReport:

    def test_coverage_ratio_all_chash(self) -> None:
        from nexus.doc.citations import grounding_report, scan_citations

        h = "a" * 64
        md = f"[x](chash:{h}) and [y](chash:{h})"
        report = grounding_report(scan_citations(md))
        assert report.chash_count == 2
        assert report.total == 2
        assert report.coverage == 1.0

    def test_coverage_ratio_mixed(self) -> None:
        from nexus.doc.citations import grounding_report, scan_citations

        h = "f" * 64
        md = (
            f"[x](chash:{h}) — three prose: [Grossberg 2013], [Ashby 1956], [12]."
        )
        report = grounding_report(scan_citations(md))
        assert report.chash_count == 1
        assert report.total == 4
        assert abs(report.coverage - 0.25) < 1e-9

    def test_coverage_zero_when_no_citations(self) -> None:
        from nexus.doc.citations import grounding_report

        report = grounding_report([])
        assert report.total == 0
        assert report.coverage == 0.0
        assert report.chash_count == 0


# ── check-extensions logic ───────────────────────────────────────────────────


class TestExtensionsCheck:
    """check-extensions uses projection data to flag claims that
    don't project into a designated primary-source collection.  v1:
    doc-level check (is this doc's source title grounded in the
    primary-source collection above threshold?)."""

    def test_doc_above_threshold_not_flagged(self) -> None:
        from nexus.doc.citations import extensions_report

        fake_tax = MagicMock()
        fake_tax.chunk_grounded_in = MagicMock(return_value=0.85)

        report = extensions_report(
            doc_ids=["doc-1"],
            primary_source="docs__primary",
            threshold=0.70,
            taxonomy=fake_tax,
        )
        assert report.candidates == []
        assert report.checked == 1

    def test_doc_below_threshold_flagged(self) -> None:
        from nexus.doc.citations import extensions_report

        fake_tax = MagicMock()
        fake_tax.chunk_grounded_in = MagicMock(return_value=0.55)

        report = extensions_report(
            doc_ids=["doc-1"],
            primary_source="docs__primary",
            threshold=0.70,
            taxonomy=fake_tax,
        )
        assert report.candidates == [("doc-1", 0.55)]

    def test_doc_with_no_projection_reported_insufficient_data(self) -> None:
        from nexus.doc.citations import extensions_report

        fake_tax = MagicMock()
        fake_tax.chunk_grounded_in = MagicMock(return_value=None)

        report = extensions_report(
            doc_ids=["doc-1"],
            primary_source="docs__primary",
            threshold=0.70,
            taxonomy=fake_tax,
        )
        assert report.candidates == []
        assert report.no_data == ["doc-1"]
