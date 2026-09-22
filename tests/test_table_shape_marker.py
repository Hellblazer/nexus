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
    assert marked.startswith("[table structure suspect")
    assert "header describes 10 columns" in marked
    assert "rows carry 9" in marked
    # The table itself is preserved verbatim: the marker adds a warning, it
    # never edits or drops the values a reader may still want.
    assert KNOWFEAT_TABLE_I in marked


def test_a_row_that_lost_its_label_is_detected() -> None:
    marked, defects = mark_misshapen_tables(MERGED_ROW_LABEL)

    kinds = [d["kind"] for d in defects]
    assert "empty_row_label" in kinds, defects
    assert "[table structure suspect" in marked
    assert MERGED_ROW_LABEL in marked


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
