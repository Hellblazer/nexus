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


def test_a_table_whose_own_prose_mentions_the_marker_is_still_flagged() -> None:
    """code-review round 2. An unanchored `_MARKER_PREFIX in block` guard,
    added to keep a re-extraction from double-marking, silently skipped a
    genuinely broken table that merely MENTIONED the marker string in a
    cell, and under-reported the run summary with it. No reachable path
    feeds already-marked text back through extraction anyway, so the guard
    is gone: a duplicate marker costs a line, a silently unflagged table
    costs the whole point."""
    sneaky = KNOWFEAT_TABLE_I.replace(
        "<td>Property Semantic underst.</td>",
        "<td>Discussed under [table structure suspect] in the appendix</td>",
    )
    marked, defects = mark_misshapen_tables(sneaky)
    assert [d["kind"] for d in defects] == ["header_column_mismatch"], defects
    assert marked.startswith("<table>[table structure suspect")


# ── nexus-8eg4w: code in a table cell mis-recognized as LaTeX math ──────────
#
# Measured on the KnowFeat paper's TABLE V Code row: MinerU's formula model
# classified plain Python as LaTeX math and rendered it with every
# identifier underscore escaped. The source text cannot be recovered here
# (MinerU's model already destroyed it before this module ever sees the
# table), so the only honest move is the same one _table_shape_defects
# already makes for a structural defect: name the cell as suspect, touch
# nothing.

# A trimmed, single-cell version of the real corruption:
# "mode_counts['fan_out'] / n" rendered as escaped-underscore math.
KNOWFEAT_TABLE_V_CODE_ROW = (
    "<table><tr><td>Case 1</td><td>Case 2</td></tr>"
    "<tr><td>Code</td>"
    r"<td>$\Gamma_{}\mathrm{{mode\_counts\Gamma}[^{\prime}\fan\_out^{\prime}]}~/~\Gamma_{}\mathrm{{n}}}}$</td>"
    "</tr></table>"
)

GENUINE_MATH_TABLE = (
    "<table><tr><td>Symbol</td><td>Definition</td></tr>"
    r"<tr><td>$x_i$</td><td>the $i$-th sample, $y_j = \sum_i x_i w_{ij}$</td></tr>"
    "</table>"
)


def test_code_in_a_cell_mis_recognized_as_math_is_detected_and_marked() -> None:
    marked, defects = mark_misshapen_tables(KNOWFEAT_TABLE_V_CODE_ROW)
    assert [d["kind"] for d in defects] == ["code_like_math"], defects
    assert defects[0]["cells"] == 1
    assert "may be code" in marked
    assert "mis-recognized as math" in marked


def test_code_like_math_table_bytes_are_never_rewritten() -> None:
    """The suspect content is never edited, only prefixed -- rewriting an
    already-unrecoverable cell would be a second unverifiable
    transformation on top of the first (mark_misshapen_tables's own
    docstring, extended here to the content signal)."""
    marked, defects = mark_misshapen_tables(KNOWFEAT_TABLE_V_CODE_ROW)
    assert defects
    assert _without_marker(marked) == KNOWFEAT_TABLE_V_CODE_ROW.replace(
        "<table>", "<table>",
    )
    assert r"mode\_counts" in marked, "original (garbled) cell text must survive verbatim"


def test_genuine_math_with_subscripts_is_not_flagged() -> None:
    """Non-vacuity, false-positive direction: ordinary LaTeX subscripts
    (bare underscore, never escaped) must not trip the detector -- this is
    the Algorithm-1-survives-fine contrast the bead itself calls out."""
    marked, defects = mark_misshapen_tables(GENUINE_MATH_TABLE)
    assert marked == GENUINE_MATH_TABLE
    assert defects == []


def test_a_single_row_code_like_math_table_is_still_flagged() -> None:
    """Unlike the shape defects, this signal needs no header/data
    disagreement to fire -- a one-row table can still hold a corrupted
    code cell."""
    one_row = (
        "<table><tr><td>Code</td>"
        r"<td>$\mathrm{ratio\_fi} = \mathrm{f\_i} / (\mathrm{f\_o} + eps)$</td>"
        "</tr></table>"
    )
    marked, defects = mark_misshapen_tables(one_row)
    assert [d["kind"] for d in defects] == ["code_like_math"], defects
    assert marked.startswith("<table>[table structure suspect")


# ── nexus-8eg4w follow-up (wave-2 review, T2 nexus/critique-wave2-2026-09-23):
# false-positive measurement against REAL indexed table text, not just
# synthetic fixtures. Method: read-only store_get of 7 real papers already
# indexed in the live knowledge__* collections (both mineru- and
# docling-extracted, math-and-RL-heavy on purpose: KnowFeat, HRNN, Self-Aware
# Vector Embeddings, Verbalizable Representations/Global Workspace,
# Conservative Q-Learning, On Prediction Using Variable Order Markov Models,
# The Context-Tree Weighting Method), run through _code_like_math_defects
# directly. Measured: 41 real <table> blocks, 1 flagged -- the KnowFeat TABLE
# V row this detector exists for -- 0 false positives. Hand-checked every
# flagged cell against the source paper; all three are the same corrupted
# Python cell the bead names. Separately, the only two literal "\_"
# occurrences found anywhere in the FULL TEXT of two of these papers (outside
# any table, so neither reaches this detector regardless) were a code
# function name in prose (tf.reduce\_logsumexp()) and a URL path segment
# (paper\_files/...) -- both code/identifier-shaped, never a genuine math
# subscript. The measured rate does not support tightening the rule; these
# two real fixtures below are added as regression pins because they are the
# strongest available evidence, real corpus text rather than a synthetic
# guess at what real text looks like.

# The exact real VOMM Table 2 (context-tree weighting notation), byte-for-byte
# from mineru's stored output: genuine subscripted math (N_{0}, P_{KT}^{s}(q))
# with BARE underscores, never escaped -- real academic notation does not
# trigger the detector, because real notation does not escape the underscore
# in the first place.
REAL_VOMM_NOTATION_TABLE = '<table><tr><td>S</td><td> $N_{0}$ </td><td> $N_{1}$ </td><td> $\\underline{{\\hat{P}_{\\mathrm{KT}}^{s}(q)}}$ </td><td> $\\overline{{P_{\\mathrm{CTW}}^{s}(q)}}$ </td><td>0</td></tr><tr><td>€</td><td>3</td><td>4</td><td>63/7680</td><td>21/1024</td><td></td></tr><tr><td>0</td><td>0</td><td>3</td><td>21/64</td><td>21/64</td><td>0</td></tr><tr><td>1</td><td>3</td><td>1</td><td>7/160</td><td>3/80</td><td>1</td></tr><tr><td>00</td><td>0</td><td>0</td><td>1</td><td>1</td><td>0</td></tr><tr><td>10</td><td>0</td><td>3</td><td>21/64</td><td>21/64</td><td></td></tr><tr><td>01</td><td>2</td><td>1</td><td>1/16</td><td>1/16</td><td>1</td></tr><tr><td>11</td><td>1</td><td>0</td><td>1/2</td><td>1/2</td><td>C 1</td></tr></table>'

# The exact real KnowFeat TABLE V, byte-for-byte from mineru's stored output
# (tumbler 1.12.152, collection knowledge__semantic-operators): three
# corrupted Code cells, the bead's own true-positive case, kept whole
# (including mineru's own malformed nested <table> tags) rather than trimmed
# to a clean synthetic version.
REAL_KNOWFEAT_TABLE_V = "<table><tr><td>Case 1: Network Topology</td><td></td><td>Case 2: Cross-Dimensional Interaction</td></tr><tr><td>Feature Name</td><td> $\\pm\\mathrm{an\\_in\\_fan\\_out\\_ratio}$ </td><td>high_risk_region_off_hour_ratio</td></tr><tr><td>Description</td><td>Asymmetry between incoming and outgoing transac- tion patterns, weighted by abnormal mode proportion</td><td>Multiplicative interaction between geographic risk (border regions) and temporal anomaly (off-hour trad- ing)</td></tr><tr><td>Knowledge</td><td>RULE_006: scatter-in, gather-out layering RULE_013: pyramid-shaped fund networks PAT_A03: money mule behavioral pattern IND_15: abnormal transaction modes (fan-in, many- to-many)</td><td>RULE_005: cross-regional fund flows RULE_014: border area transaction monitoring IND_08/09: off-hour transaction concentration EXP_05: &quot;border region + nighttime trading is the hallmark of cross-border laundering&quot;</td></tr><table><tr><td>Case 1: Network Topology</td><td></td><td>Case 2: Cross-Dimensional Interaction</td></tr>\n<tr><td>Reasoning</td><td>Step 1: RULE_006/013 indicate scatter-in/gather-out and pyramid networks both manifest as fan-in/fan-out asymmetry → compute ratio per wallet. Step 2: PAT_A03 shows money mules exhibit fan-in fan-out → ratio captures core topology signal. Step 3: IND_15 identifies abnormal modes (many- to-many, one-to-many) → weight ratio by abnormal mode proportion to amplify suspicious patterns.</td><td>Step 1: RULE_005/014 flag border regions (Tibet, Xinjiang) as high-risk for cross-regional flows → create binary geographic indicator. Step 2: IND_08/09 flag off-hour (18:0008:00) con- centration as suspicious → compute off-hour transac- tion ratio. Step 3: EXP_05 states neither signal alone is sufficient → multiply indicators so only the conjunction triggers a high score.</td></tr><table><tr><td>Case 1: Network Topology</td><td></td><td>Case 2: Cross-Dimensional Interaction</td></tr>\n<tr><td>Code</td><td> $\\mathrm{~\\underline{{~f~i~}~}~}=\\mathrm{~mode\\_counts~}[\\mathrm{~'~fan\\_in^{\\prime}~}]\\mathrm{~/~}\\mathrm{~n~}$   $\\Gamma_{}\\mathrm{{o}~\\Gamma_{}\\mathrm{{o}~\\Gamma_{}\\mathrm{{mode\\_counts\\Gamma}[^{\\prime}\\fan\\_out^{\\prime}]}~/~\\Gamma_{}\\mathrm{{n}}}}$   $\\mathtt{ratio\\_fi}\\mathtt{\\Gamma}_{}{\\mathtt{f}}_{}{\\mathtt{i}}_{}\\mathtt{\\Gamma}_{}{}/\\mathtt{\\Gamma}_{}{\\mathtt{(f}}_{}{\\mathtt{0}}\\mathtt{\\Gamma}_{}+\\mathtt{\\eps}\\mathtt{)}_{}$   $\\mathsf{abnormal}~=~\\mathsf{fi}~+~\\mathsf{m}2\\mathsf{m}~+~\\mathsf{o}2\\mathsf{m}$   $\\mathtt{result\\=\\ratio\\\\star\\(1+\\abnormal)}$ </td><td> $\\mathrm{i}s\\_\\mathrm{hr}\\=\\\\mathrm{region.i}s\\mathrm{i}\\mathrm{n}([540000,right.$  650000])  $\\begin{array}{l}{\\mathsf{off}\\\\mathsf{\\Gamma}=\\mathrm{~\\mathsf{\\Gamma}~(hour~\\mathsf{\\Gamma}>=~\\mathsf{\\Omega}1\\otimes\\mathsf{\\Gamma}|~\\mathsf{\\Gamma}~(hour~\\mathsf{\\Omega}<~\\mathsf{\\Omega}8)~}}\\end{array}$   $\\cot\\pounds\\_{\\bf{ratio\\textit{\\textbf{\\cot}}}}=\\cot\\pounds.\\s\\mathsf{um(\\cdot)}\\/\\mathrm{~\\textit~{~n~}~}$   $\\mathtt{result\\=\\\\mathrm{i}s\\_hr\\mathrm{\\Sigma}\\star\\\\mathrm{off\\_ratio}}$ </td></tr><tr><td>Data Fields Semantic Dim.</td><td>transaction_mode, src, dst</td><td>region, hour</td></tr><tr><td>L1: Execution</td><td>Entity Association</td><td>Time &amp; Geography × Entity Association</td></tr><tr><td>L2: Statistical</td><td>PASS (0.8s, shape: 200K× 1, no NaN)</td><td>PASS (0.3s, shape: 200K×1, no NaN)</td></tr><table><tr><td>Case 1: Network Topology</td><td></td><td>Case 2: Cross-Dimensional Interaction</td></tr>\n<tr><td></td><td> $\\mathrm{IV}{=}0.34~(>0.02),~\\mathrm{KS}{=}0.28~(>0.05),$  max corr.=0.41 (&lt;0.7), missing=0%</td><td> $\\mathrm{IV=0.19~(>0.02),~KS=0.15~(>0.05),}$  max corr.=0.23 (&lt;0.7), missing=0%</td></tr><tr><td>L3: Model</td><td>∆Recall=+0.38%, Prec.=97.2% (≥90%) ACCEPTED</td><td>∆Recall=+0.21%, Prec.=96.8% (≥90%) ACCEPTED</td></tr></table>"


def test_real_vomm_notation_table_is_not_flagged() -> None:
    """False-positive check on real corpus text: genuine LaTeX subscripts
    in a real notation table, bare underscore, never escaped. Measured
    2026-09-23 across 41 real <table> blocks from 7 live papers: 0 false
    positives; this is that measurement's strongest single case."""
    marked, defects = mark_misshapen_tables(REAL_VOMM_NOTATION_TABLE)
    assert marked == REAL_VOMM_NOTATION_TABLE
    assert defects == []


def test_real_knowfeat_table_v_is_flagged() -> None:
    """True-positive check on real corpus text: the actual mineru output
    for the bead's own motivating case, unmodified. This real table also
    happens to trip empty_row_label (a genuine, separate structural
    defect in the same messy real HTML, one of its rows has lost its
    label) -- both are real defects mark_misshapen_tables should report,
    so the assertion checks code_like_math is among them rather than
    requiring it be the only one."""
    marked, defects = mark_misshapen_tables(REAL_KNOWFEAT_TABLE_V)
    kinds = [d["kind"] for d in defects]
    assert "code_like_math" in kinds, defects
    code_defect = next(d for d in defects if d["kind"] == "code_like_math")
    assert code_defect["cells"] == 3
    assert marked.startswith("<table>[table structure suspect")
