"""nexus-jd8fi: MinerU image references become caption-anchored text markers,
and tables MinerU rendered as text populate ``table_regions``.

Measured on the Pangram 4 Technical Report (2026-09-03): with
``table_enable=false`` MinerU emitted 7 ``![](images/<sha>.jpg)`` lines, 0
``<table>`` blocks and 4515 chars for pages 18-21; with it on, 1 image ref,
6 tables and 13890 chars. The indexed copy carried no tabular value and no
signal that any was missing.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.pdf_extractor import (
    PDFExtractor,
    _mark_unextracted_visuals,
    _visual_label,
)

_REF = "![](images/3cdad948d7c4180cc2f036ae398fb2366c0c0bda7e554f88ec2e6ae4f8ba9a0e.jpg)"


def _table_entry(caption: str = "Table 6. Prediction distribution.", body: str = "") -> dict:
    return {
        "type": "table",
        "img_path": "images/3cdad948d7c4180cc2f036ae398fb2366c0c0bda7e554f88ec2e6ae4f8ba9a0e.jpg",
        "table_caption": [caption],
        "table_footnote": [],
        "table_body": body,
        "page_idx": 0,
    }


def _image_entry(caption: str = "Figure 5. Pangram 3.3.2 vs. 4 on an excerpt.") -> dict:
    return {
        "type": "image",
        "img_path": "images/3cdad948d7c4180cc2f036ae398fb2366c0c0bda7e554f88ec2e6ae4f8ba9a0e.jpg",
        "image_caption": [caption],
        "image_footnote": [],
        "page_idx": 0,
    }


class TestMarkUnextractedVisuals:
    def test_table_without_body_becomes_caption_anchored_marker(self) -> None:
        md = f"interval of (0.0008%,\n{_REF}  \nTable 6. Prediction distribution.\n"
        out = _mark_unextracted_visuals(md, [_table_entry()])
        assert "![](" not in out
        assert "[Table 6 not extracted as text; values not indexed]" in out
        # Surrounding prose and the caption line are untouched.
        assert out.startswith("interval of (0.0008%,\n")
        assert "Table 6. Prediction distribution." in out

    def test_figure_becomes_image_marker(self) -> None:
        out = _mark_unextracted_visuals(f"{_REF}  \nFigure 5. Caption.", [_image_entry()])
        assert out == "[Figure 5 is an image; not indexed as text]  \nFigure 5. Caption."

    def test_reference_with_no_content_list_entry_gets_generic_marker(self) -> None:
        out = _mark_unextracted_visuals(f"a\n{_REF}\nb", [])
        assert out == "a\n[image not indexed as text]\nb"

    def test_matches_on_basename_when_bucket_prefix_differs(self) -> None:
        entry = _table_entry()
        entry["img_path"] = "/srv/mineru/out/images/3cdad948d7c4180cc2f036ae398fb2366c0c0bda7e554f88ec2e6ae4f8ba9a0e.jpg"
        out = _mark_unextracted_visuals(_REF, [entry])
        assert out == "[Table 6 not extracted as text; values not indexed]"

    def test_uncaptioned_table_falls_back_to_bare_kind(self) -> None:
        entry = _table_entry(caption="")
        out = _mark_unextracted_visuals(_REF, [entry])
        assert out == "[Table not extracted as text; values not indexed]"

    def test_html_tables_and_plain_text_pass_through(self) -> None:
        md = "<table><tr><td>Turnitin</td><td>0.01%</td></tr></table>\n\nTable 12. FPR."
        assert _mark_unextracted_visuals(md, [_table_entry(body="<table>...</table>")]) == md

    def test_every_reference_in_a_page_is_replaced(self) -> None:
        md = "\n".join([_REF, "text", "![alt](images/other.jpg)", "more"])
        out = _mark_unextracted_visuals(md, [_image_entry()])
        assert "![" not in out
        assert out.count("not indexed as text") == 2

    def test_reference_with_no_content_list_entry_but_a_figure_caption_is_labelled(self) -> None:
        """nexus-9zly6 GAP 1: when the content_list lookup misses, derive
        the label from the caption line immediately under the image
        reference in the markdown itself, so the marker reads like the
        labelled branch instead of the bare generic one."""
        md = f"{_REF}  \nFig. 2. A chart of per-method average ranks.\n"
        out = _mark_unextracted_visuals(md, [])
        assert out == "[Fig. 2 is an image; not indexed as text]  \nFig. 2. A chart of per-method average ranks.\n"

    def test_reference_with_no_content_list_entry_but_a_chart_caption_is_labelled(self) -> None:
        md = f"{_REF}  \nChart 4. Throughput by batch size.\n"
        out = _mark_unextracted_visuals(md, [])
        assert out == "[Chart 4 is an image; not indexed as text]  \nChart 4. Throughput by batch size.\n"

    def test_reference_with_no_content_list_entry_and_no_caption_still_gets_generic_marker(self) -> None:
        """The existing (unlabelled) behaviour must survive when there is
        genuinely no caption to derive a label from."""
        out = _mark_unextracted_visuals(f"a\n{_REF}\nb", [])
        assert out == "a\n[image not indexed as text]\nb"

    # ── adversarial caption-adjacency cases (round-2 critique) ──────────────
    #
    # A wrong specific label is worse than the generic marker: a caption is
    # only ever trusted for THIS reference when it (a) sits directly under
    # it, with no other image/block between, and (b) matches the FIGURE
    # pattern (Fig./Figure/Chart) -- never Table, since these bare "![...]"
    # references are presented to the reader as images and a table label
    # would misdescribe what MinerU actually rendered.

    def test_two_images_whose_captions_come_after_both_never_mislabel_the_second(self) -> None:
        """Adversarial case 1. Layout is image, image, caption, caption --
        not MinerU's normal image-then-own-caption pairing. The first
        reference already falls back correctly today (its own next line is
        another image ref, which never matches the figure pattern); the
        SECOND reference used to accept "Fig. 1", which is really the
        first image's caption, as its own -- a wrong, specific mislabel.
        Both must fall back to the generic marker.

        Fails before the fix: the second marker reads
        "[Fig. 1 is an image; not indexed as text]" instead of generic.
        """
        ref2 = "![](images/other.jpg)"
        md = f"{_REF}\n{ref2}\nFig. 1. Caption A.\nFig. 2. Caption B.\n"
        out = _mark_unextracted_visuals(md, [])
        assert out.count("[image not indexed as text]") == 2
        assert "Fig. 1" not in out.split("\n")[0]
        assert "Fig. 1" not in out.split("\n")[1]

    def test_image_followed_by_a_table_caption_is_never_labelled_as_a_table(self) -> None:
        """Adversarial case 2. A "Table N" caption directly under a bare
        image reference must never produce a table-shaped marker for it --
        these references are presented as images; mislabelling one as a
        table both changes what kind of gap the marker claims and attaches
        a wrong specific number to it.

        Fails before the fix: produces
        "[Table 9 not extracted as text; values not indexed]".
        """
        md = f"{_REF}  \nTable 9. Per-method average ranks.\n"
        out = _mark_unextracted_visuals(md, [])
        assert out == "[image not indexed as text]  \nTable 9. Per-method average ranks.\n"
        assert "Table 9 not extracted" not in out

    def test_caption_separated_from_the_image_by_a_paragraph_is_not_taken(self) -> None:
        """Adversarial case 3. Already handled correctly by the existing
        first-line-only lookahead: _caption_label_after only ever inspects
        the text up to the first newline after the reference, so an
        unrelated paragraph sitting between the reference and a later
        caption is never skipped over to reach it. Included here as a
        pinned regression, not a fix -- this one does not fail before the
        round-2 change.
        """
        md = f"{_REF}\n\nSome unrelated paragraph text.\n\nFig. 1. Caption.\n"
        out = _mark_unextracted_visuals(md, [])
        assert "[image not indexed as text]" in out
        assert "Fig. 1 is an image" not in out

    def test_content_list_entry_with_no_markdown_reference_still_gets_a_marker(self) -> None:
        """nexus-9zly6: covers the 'if not md or \"![\" not in md: return
        md' early return. MinerU's content_list and its page markdown are
        two independently produced outputs; content_list can name a visual
        with NO corresponding '![...]' placeholder anywhere in the page's
        markdown at all. That used to be a silent, unmarked hole -- worse
        than the generic marker, since there was not even a bracketed gap
        to query against."""
        md = "Plain prose with no image reference at all."
        out = _mark_unextracted_visuals(md, [_image_entry()])
        assert "![" not in out
        assert "[Figure 5 is an image; not indexed as text]" in out
        assert out.startswith("Plain prose with no image reference at all.")

    def test_content_list_entry_referenced_in_md_is_not_also_appended_as_an_orphan(self) -> None:
        """Non-vacuity for the orphan-marker pass above: an entry that DID
        match a reference must not ALSO get a trailing duplicate marker."""
        out = _mark_unextracted_visuals(_REF, [_image_entry()])
        assert out.count("not indexed as text") == 1

    def test_orphan_entry_with_no_img_path_still_gets_a_marker(self) -> None:
        """Round-2 critique: the prior orphan pass keyed everything off
        img_path, so an entry that has none (never matchable to any
        "![...]" reference by construction) was silently skipped --
        reproducing the exact class of bug this bead closes. Every
        image/figure entry with no matching markdown reference must
        produce a marker whether or not it carries an img_path.

        Fails before the fix: out == md unchanged, no marker at all.
        """
        entry = _image_entry()
        del entry["img_path"]
        md = "Plain prose, no image reference anywhere."
        out = _mark_unextracted_visuals(md, [entry])
        assert "[Figure 5 is an image; not indexed as text]" in out

    def test_orphan_equation_entry_with_no_img_path_gets_no_marker(self) -> None:
        """Non-regression guard for the fix above: an "equation" content_list
        entry (MinerU's LaTeX-formula category) never carries an img_path
        either, but it is not a figure -- it must NOT be swept into the
        orphan-marker pass and mislabelled as an unindexed image."""
        entry = {"type": "equation", "text": "$E=mc^2$"}
        md = "Plain prose, no image reference anywhere."
        out = _mark_unextracted_visuals(md, [entry])
        assert out == md


class TestVisualLabel:
    def test_table_arabic(self) -> None:
        assert _visual_label({"table_caption": ["Table 12. Detectors."]}, "Table") == "Table 12"

    def test_table_roman(self) -> None:
        assert _visual_label({"table_caption": ["Table I: Results"]}, "Table") == "Table I"

    def test_figure_fig_abbrev_with_sublabel(self) -> None:
        assert _visual_label({"image_caption": ["Fig. 3a shows"]}, "Figure") == "Fig. 3a"

    def test_no_label_returns_kind(self) -> None:
        assert _visual_label({"image_caption": ["A photograph."]}, "Figure") == "Figure"
        assert _visual_label({}, "Table") == "Table"


def _one_page_pdf_ctx(pages: int = 1) -> MagicMock:
    doc = MagicMock()
    doc.__len__.return_value = pages
    ctx = MagicMock()
    ctx.__enter__.return_value = doc
    ctx.__exit__.return_value = False
    return ctx


def _make_mock_docling(pages: list[str]):
    mock_doc = MagicMock()
    mock_doc.num_pages.return_value = len(pages)
    mock_doc.export_to_markdown.side_effect = pages
    mock_doc.iterate_items.return_value = iter([])
    mock_result = MagicMock()
    mock_result.document = mock_doc
    mock_converter = MagicMock()
    mock_converter.convert.return_value = mock_result
    return mock_converter, mock_doc


class TestDoclingExtractionMarksUnextractedFigures:
    """nexus-9zly6 GAP 2: ``_extract_with_docling`` never called the marker
    pass at all, so a docling-extracted PDF's figures vanished with no
    signal, unlike the MinerU path. Docling has no MinerU content_list, and
    its own documented image placeholder is ``<!-- image -->``
    (docling_core.types.doc.document's ``export_to_markdown(... ,
    image_placeholder="<!-- image -->")`` default), not MinerU's
    ``![](images/<sha>.jpg)`` -- a different shape, mocked here the same
    way every other docling test in this suite mocks
    ``doc.export_to_markdown`` rather than booting the real model.
    """

    def test_docling_page_with_an_image_placeholder_and_caption_is_labelled(self, tmp_path) -> None:
        page_md = "Prose before.\n\n<!-- image -->\n\nFig. 7. A labelled chart.\n\nProse after."
        mock_converter, _ = _make_mock_docling([page_md])
        ext = PDFExtractor()
        ext._converter_enriched = mock_converter

        with patch.object(ext, "_extract_title", return_value=""):
            result = ext._extract_with_docling(tmp_path / "doc.pdf")

        assert "<!-- image -->" not in result.text
        assert "[Fig. 7 is an image; not indexed as text]" in result.text
        assert "Prose before." in result.text
        assert "Prose after." in result.text

    def test_docling_page_with_an_image_placeholder_and_no_caption_gets_generic_marker(self, tmp_path) -> None:
        page_md = "Prose.\n\n<!-- image -->\n\nNo caption follows."
        mock_converter, _ = _make_mock_docling([page_md])
        ext = PDFExtractor()
        ext._converter_enriched = mock_converter

        with patch.object(ext, "_extract_title", return_value=""):
            result = ext._extract_with_docling(tmp_path / "doc.pdf")

        assert "<!-- image -->" not in result.text
        assert "[image not indexed as text]" in result.text


class TestMineruExtractionCarriesMarkersAndTableRegions:
    """Through ``_extract_with_mineru`` with the isolated runner patched, so
    the marker pass and the ``table_regions`` population are exercised where
    they live, not only as free functions."""

    def test_marker_lands_in_result_text_and_extracted_table_reaches_table_regions(self) -> None:
        ext = PDFExtractor()
        html = "<table><tr><td>Model</td><td>Turnitin</td></tr></table>"
        page0_md = f"Prose.\n{_REF}  \nTable 6. Caption.\n"
        page1_md = f"{html}\n\nTable 12. Detectors.\n"
        page0_cl = [_table_entry()]
        page1_cl = [{**_table_entry(caption="Table 12. Detectors.", body=html),
                     "img_path": "images/aa.jpg"}]

        def fake_isolated(pdf_path, start, end):
            return ((page0_md, page0_cl, [{}]) if start == 0 else (page1_md, page1_cl, [{}]))

        with (
            patch("nexus.pdf_extractor.do_parse", object()),
            patch("pymupdf.open", return_value=_one_page_pdf_ctx(2)),
            patch("nexus.config.get_mineru_page_batch", return_value=1),
            patch.object(ext, "_mineru_run_isolated", side_effect=fake_isolated),
        ):
            result = ext._extract_with_mineru(Path("/tmp/paper.pdf"))

        assert "![](" not in result.text
        assert "[Table 6 not extracted as text; values not indexed]" in result.text
        assert "Turnitin" in result.text
        # The rendered table on page 2 (0-based batch page 1, rebased from
        # MinerU's batch-relative page_idx 0) is the only table region.
        assert result.metadata["table_regions"] == [{"page": 2, "html": html}]
        assert result.metadata["extraction_method"] == "mineru"
