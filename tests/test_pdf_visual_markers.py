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
