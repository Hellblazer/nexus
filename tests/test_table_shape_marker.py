# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-stkek: a captured table whose rows disagree with its header is marked.

MinerU stores the HTML it produced for a table verbatim, and nothing checked
its shape. Measured on the KnowFeat paper (tumbler 1.12.152, page 3): TABLE I
is a 7-method by 7-property support matrix whose first header line collapsed
into ``<tr><td colspan="10">Open Auto CAA Feat LLM- OC</td></tr>``, dropping
the seventh method name outright. Every value row is then headerless, and
three of the seven rows read as entirely blank — including "Provenance
track.", the one property the paper exists to claim. The stored table states
the opposite of the source, with nothing saying so.

Two distinct mechanisms need two distinct signals, and the second one is why
a cell-count check alone is not enough:

* **Header collapse** (TABLE I): the header's effective column count, with
  ``colspan`` summed, disagrees with the data rows'.
* **Row-label merge** (TABLE V, page 8): "Data Fields" and "Semantic Dim."
  merged into one label cell, shifting every later row up by one, so
  ``L1: Execution`` carries the Semantic Dim. values and ``L2: Statistical``
  carries L1's. Cell COUNTS are unchanged by this, so the first signal
  cannot see it; the orphaned row left with an empty label cell can.

This marks and does not refuse. A malformed table in an otherwise clean
extraction is not garbage text, so this is deliberately not wired into the
post-extraction quality gate: failing an 11-page document over one of its 13
tables would be overridden by habit, and a gate that is always overridden
protects nothing.
"""
from __future__ import annotations

import re

from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import mark_misshapen_tables

# The real TABLE I shape, trimmed to four properties. The header's colspan
# says ten columns; every data row carries nine cells.
KNOWFEAT_TABLE_I = (
    '<table><tr><td colspan="10">Open Auto CAA Feat LLM- OC</td></tr>'
    "<tr><td>Property Semantic underst.</td><td>FE</td><td>Feat</td><td>FE ✓</td>"
    "<td>LLM</td><td>FE ✓</td><td></td><td>Tree ✓</td><td>Feat</td></tr>"
    "<tr><td>Statistical filt.</td><td></td><td></td><td></td><td></td><td></td>"
    "<td></td><td></td><td></td></tr>"
    "<tr><td>Provenance track.</td><td></td><td></td><td></td><td></td><td></td>"
    "<td></td><td></td><td></td></tr></table>"
)

WELL_FORMED = (
    "<table><tr><td>Dataset</td><td>No-FE</td><td>KnowFeat</td></tr>"
    "<tr><td>credit_g</td><td>.749</td><td>.752</td></tr>"
    "<tr><td>diabetes</td><td>.812</td><td>.810</td></tr></table>"
)

# TABLE V's shape: consistent cell counts, but one row lost its label when
# two labels merged into the cell above.
MERGED_ROW_LABEL = (
    "<table><tr><td>Case 1</td><td>Case 2</td></tr>"
    "<tr><td>Data Fields Semantic Dim.</td><td>region, hour</td></tr>"
    "<tr><td>L1: Execution</td><td>Entity Association</td></tr>"
    "<tr><td></td><td>IV=0.19, KS=0.15</td></tr>"
    "<tr><td>L3: Model</td><td>ACCEPTED</td></tr></table>"
)


def _without_marker(text: str) -> str:
    """*text* with any suspect-table marker removed, for asserting the table's
    own bytes were untouched."""
    return re.sub(r"\[table structure suspect[^\]]*\]", "", text)


def test_a_well_formed_table_is_left_exactly_alone() -> None:
    marked, defects = mark_misshapen_tables(WELL_FORMED)
    assert marked == WELL_FORMED
    assert defects == []


def test_the_header_collapse_is_detected_and_marked() -> None:
    marked, defects = mark_misshapen_tables(KNOWFEAT_TABLE_I)

    assert len(defects) == 1, defects
    assert defects[0]["kind"] == "header_column_mismatch"
    assert defects[0]["header_columns"] == 10
    assert defects[0]["row_columns"] == 9
    assert "[table structure suspect" in marked
    assert "header describes 10 columns" in marked
    assert "rows carry 9" in marked
    # The table itself is preserved verbatim: the marker adds a warning, it
    # never edits or drops the values a reader may still want. Stripping the
    # marker restores the original block byte for byte.
    assert _without_marker(marked) == KNOWFEAT_TABLE_I


def test_a_row_that_lost_its_label_is_detected() -> None:
    marked, defects = mark_misshapen_tables(MERGED_ROW_LABEL)

    kinds = [d["kind"] for d in defects]
    assert "empty_row_label" in kinds, defects
    assert "[table structure suspect" in marked
    assert _without_marker(marked) == MERGED_ROW_LABEL


def test_a_table_label_is_named_in_the_marker_when_the_caption_has_one() -> None:
    """The marker is text the embedder sees, so a query for the table lands on
    the warning as well as the values — the same reasoning as the existing
    unextracted-visual markers."""
    text = "TABLE I\nCOMPARISON OF METHODS.\n" + KNOWFEAT_TABLE_I
    marked, _ = mark_misshapen_tables(text)
    assert "TABLE I" in marked.split("[table structure suspect")[1][:80]


def test_several_tables_are_judged_independently() -> None:
    marked, defects = mark_misshapen_tables(WELL_FORMED + "\n\nprose\n\n" + KNOWFEAT_TABLE_I)
    assert len(defects) == 1
    assert marked.count("[table structure suspect") == 1
    assert WELL_FORMED in marked


def test_text_with_no_table_is_returned_unchanged() -> None:
    prose = "Algorithm 1 formalizes the pipeline and Figure 1 illustrates it."
    assert mark_misshapen_tables(prose) == (prose, [])


def test_a_single_row_table_is_not_judged_against_itself() -> None:
    """One row is a header with no data rows to disagree with it. Flagging it
    would fire on every stub table for no reader benefit."""
    one_row = "<table><tr><td>a</td><td>b</td></tr></table>"
    assert mark_misshapen_tables(one_row) == (one_row, [])


def test_a_ragged_table_reports_the_modal_row_width() -> None:
    """Real tables carry the odd merged cell. The comparison is against the
    modal data-row width, so one irregular row does not decide the verdict,
    and a header that disagrees with the majority still does."""
    ragged = (
        '<table><tr><td colspan="4">H</td></tr>'
        "<tr><td>a</td><td>b</td></tr>"
        "<tr><td>c</td><td>d</td></tr>"
        "<tr><td>e</td></tr></table>"
    )
    _marked, defects = mark_misshapen_tables(ragged)
    assert len(defects) == 1
    assert defects[0]["header_columns"] == 4
    assert defects[0]["row_columns"] == 2


# ── review round 1: nexus-stkek marker placement and labelling ──────────────

def test_the_label_is_the_last_NUMBERED_caption_not_the_last_word_table() -> None:
    """code-review finding: the label was read by rfind("table") over the
    lookback window, which can land on a different occurrence than the one
    the regex matched. A bare, unnumbered "table" mention after the real
    caption then produced a label like "table b"."""
    text = (
        "TABLE I\nCOMPARISON OF METHODS.\n"
        "We also compare with OCTree in the related work table below.\n"
        + KNOWFEAT_TABLE_I
    )
    marked, defects = mark_misshapen_tables(text)
    assert defects[0]["label"] == "TABLE I", defects[0]["label"]
    assert "(TABLE I)" in marked


def test_the_marker_rides_inside_the_table_so_continuation_chunks_keep_it() -> None:
    """substantive-critic finding: with the marker on its own line BEFORE
    <table>, a table big enough to trip PDFChunker's table_break left the
    marker in the preceding chunk and none of the table's own chunks. The
    marker now sits just inside the opening tag, which is the span
    _table_header re-injects into every continuation chunk."""
    marked, _ = mark_misshapen_tables(KNOWFEAT_TABLE_I)
    assert not marked.startswith("[table structure suspect"), (
        "a marker before the tag is what separated it from the table"
    )
    assert marked.startswith("<table>[table structure suspect")


def test_every_chunk_of_a_flagged_oversized_table_carries_the_marker() -> None:
    """The property the docstring claims, pinned against the real chunker:
    a query that surfaces any part of the table surfaces the warning."""
    rows = "".join(
        f"<tr><td>row {i:03d}</td><td>{i * 3}</td><td>{i * 7}</td></tr>" for i in range(90)
    )
    big = '<table><tr><td colspan="9">collapsed header</td></tr>' + rows + "</table>"
    text = "prose before the table.\n\nTABLE VII\nA LARGE TABLE.\n" + big + "\n\nprose after."
    marked, defects = mark_misshapen_tables(text)
    assert defects, "fixture must be flagged, or this pins nothing"

    chunks = PDFChunker(chunk_chars=700).chunk(marked, {})
    body = [c for c in chunks if "<tr><td>row " in c.text]
    assert len(body) > 1, "fixture must split the table across chunks"
    missing = [c.chunk_index for c in body if "[table structure suspect" not in c.text]
    assert missing == [], f"table chunks without the marker: {missing}"


def test_an_already_marked_table_is_not_marked_twice() -> None:
    once, _ = mark_misshapen_tables(KNOWFEAT_TABLE_I)
    twice, defects = mark_misshapen_tables(once)
    assert twice == once
    assert defects == []
