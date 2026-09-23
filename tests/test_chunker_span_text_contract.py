# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hno4p: cross-chunker contract — a writer's adjacent-chunk spans
must agree with its adjacent-chunk text, in both directions, for every
chunker that writes ``chunk_start_char``/``chunk_end_char``.

Synthesised from four review rounds across nexus-kas9u, nexus-stkek and
nexus-yz7se (see nexus-hno4p's own description): no chunker declares this
contract as a type or a check, only as prose in a docstring
(:func:`nexus.catalog.store_hook.join_manifest_parts`). This module makes
it checkable, independently of that reader — the assertions below
reimplement the "confirmed overlap" search rather than importing the
reader's own trim loop, so a divergence between what a WRITER emits and
what the READER assumes is still caught even if a future edit to the
reader's algorithm and a future edit to a writer's algorithm drift in the
same wrong direction together.

THE CONTRACT, per adjacent chunk pair ``(prev, cur)`` in position order:

* ``prev_start < cur_start < prev_end`` (spans CLAIM an overlap) if and
  only if the text at the boundary really shows a matching run of at
  least :data:`MIN_VERIFIED_OVERLAP` characters (the reader's own
  threshold below which an agreement is as likely to be punctuation
  coincidence as a real overlap — mirrored here, not imported, for the
  same independence reason as above).
* Otherwise (spans say ABUT, or record no span at all) the text must NOT
  show an unclaimed duplicate run of that length. This is the yz7se shape
  specifically: a writer whose recorded span understates a real
  duplication, which a reader trusting spans-as-truth would then leave in
  the rebuild forever (``join_manifest_parts`` requires the window to
  ADVANCE before it will even attempt a trim).

ONE DOCUMENTED EXCEPTION, both ways, carried over from
``join_manifest_parts``'s own docstring: a chunk that opens by re-quoting
a table's header row (:meth:`nexus.pdf_chunker.PDFChunker._table_header`)
is not required to confirm an overlap even when one might be claimed,
because the header text — not the true preceding content — sits at its
front. No PDFChunker fixture below is known to reach that exact case (see
the module comment in ``pdf_chunker.py`` and this file's own trace notes),
but the exemption is threaded through regardless so a future PDFChunker
change that does reach it does not turn this file into a false-positive
generator.

THE TRAP (nexus-hno4p's own words): "a fixture that does not reach the
branch reports agreement indistinguishable from a fixture that does."
Every regime below is therefore paired with a KNOWN-BAD variant — the
contract checker run against a version of the SAME real chunks with one
span deliberately corrupted — asserting the checker actually flags it.
A contract test with no failing case proves nothing about its own power.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.chunker import _line_chunk, chunk_file
from nexus.md_chunker import SemanticMarkdownChunker
from nexus.pdf_chunker import PDFChunker

# Mirrors nexus.catalog.store_hook.MIN_VERIFIED_OVERLAP. Pinned here
# independently (not imported) — see the module docstring for why.
MIN_VERIFIED_OVERLAP = 12

# Mirrors nexus.catalog.store_hook._MARKDOWN_HEADING_RE (the md_chunker
# _split_large_section re-injected-heading exception).
_MD_HEADING_RE = re.compile(r"^(#{1,6} .+)\n\n")

Span = tuple[str, int | None, int | None]


def _confirmed_overlap(prev_text: str, text: str, cap: int, *, strip_heading: bool) -> int:
    """Longest suffix-of-*prev_text* / prefix-of-*text* agreement, capped
    at *cap* (the pair's recorded overlap). Below :data:`MIN_VERIFIED_OVERLAP`
    an agreement is not trusted as a real overlap (matches the reader's own
    floor). When *strip_heading* and both texts open with the SAME markdown
    ATX heading line, the heading is stripped from *text* before matching —
    md_chunker's ``_split_large_section`` re-prepends the section heading to
    every sub-chunk it emits, which is not part of the recorded overlap span.
    """
    if cap < MIN_VERIFIED_OVERLAP:
        return 0
    match_text = text
    if strip_heading:
        cur = _MD_HEADING_RE.match(text)
        prev = _MD_HEADING_RE.match(prev_text)
        if cur and prev and cur.group(1) == prev.group(1):
            match_text = text[cur.end():]
    upper = min(cap, len(match_text), len(prev_text))
    for k in range(upper, MIN_VERIFIED_OVERLAP - 1, -1):
        if prev_text.endswith(match_text[:k]):
            return k
    return 0


def _table_continuation_exempt(text: str) -> bool:
    """The one documented exception: a chunk opening with a re-quoted
    table header (PDFChunker._table_header) is not required to confirm
    an overlap the spans may claim."""
    return text.lstrip().lower().startswith("<table")


def check_adjacent_span_text_contract(
    chunks: list[Span], *, writer: str, strip_heading: bool = False,
) -> list[str]:
    """Return the list of contract violations for *chunks* (position
    order), empty when the writer's spans and text agree everywhere.

    Exposed as a plain function (not a bare ``assert``) so both the
    positive tests and the known-bad non-vacuity tests can inspect the
    violation list directly instead of parsing a pytest failure message.
    """
    violations: list[str] = []
    for i in range(1, len(chunks)):
        prev_text, prev_start, prev_end = chunks[i - 1]
        text, start, end = chunks[i]
        if prev_start is None or prev_end is None or start is None:
            continue
        spans_claim_overlap = prev_start < start < prev_end
        if spans_claim_overlap:
            recorded = prev_end - start
            confirmed = _confirmed_overlap(
                prev_text, text, recorded, strip_heading=strip_heading,
            )
            if confirmed < min(recorded, MIN_VERIFIED_OVERLAP) and not (
                _table_continuation_exempt(text)
            ):
                violations.append(
                    f"[{writer}] pair {i - 1}->{i}: spans claim a "
                    f"{recorded}-char overlap ({prev_start},{prev_end}) -> "
                    f"({start},{end}) but the text confirms only "
                    f"{confirmed} chars"
                )
        else:
            unclaimed = _confirmed_overlap(
                prev_text, text, min(len(prev_text), len(text)),
                strip_heading=strip_heading,
            )
            if unclaimed >= MIN_VERIFIED_OVERLAP:
                violations.append(
                    f"[{writer}] pair {i - 1}->{i}: spans record no "
                    f"overlap (({prev_start},{prev_end}) -> ({start},{end})) "
                    f"but the text duplicates {unclaimed} unclaimed chars"
                )
    return violations


def assert_adjacent_span_text_contract(
    chunks: list[Span], *, writer: str, strip_heading: bool = False,
) -> None:
    violations = check_adjacent_span_text_contract(
        chunks, writer=writer, strip_heading=strip_heading,
    )
    assert not violations, "\n".join(violations)


# ── shared line-offset helper (mirrors prose_indexer.py / code_indexer.py) ──


def _line_offsets(content: str) -> list[int]:
    offsets = [0]
    for i, ch in enumerate(content):
        if ch == "\n":
            offsets.append(i + 1)
    return offsets


def _char_span_for_lines(
    content: str, offsets: list[int], line_start: int, line_end: int,
) -> tuple[int, int]:
    start = offsets[line_start - 1] if 0 < line_start <= len(offsets) else 0
    end = offsets[line_end] if line_end < len(offsets) else len(content)
    return start, end


# ── pdf_chunker.PDFChunker ───────────────────────────────────────────────────


def _pdf_spans(chunker: PDFChunker, text: str) -> list[Span]:
    chunks = chunker.chunk(text, {})
    return [
        (c.text, c.metadata["chunk_start_char"], c.metadata["chunk_end_char"])
        for c in chunks
    ]


def test_pdf_chunker_adjacent_spans_agree_with_text() -> None:
    text = " ".join(f"Sentence number {i:04d} carries its own content." for i in range(400))
    chunker = PDFChunker(chunk_chars=400, overlap_percent=0.3)
    spans = _pdf_spans(chunker, text)
    assert len(spans) >= 3, "fixture must force real multi-chunk overlap"
    assert_adjacent_span_text_contract(spans, writer="pdf_chunker.PDFChunker")


def test_pdf_chunker_with_table_adjacent_spans_agree_with_text() -> None:
    """nexus-stkek precedent: a chunk boundary that runs through a table
    must still honour the contract — table breaks force an exact abut
    (no overlap claimed), and the header-continuation chunk's injected
    text must not register as an unclaimed duplicate of the prior chunk."""
    rows = "\n".join(f"<tr><td>row-{i:04d}</td><td>value-{i:04d}</td></tr>" for i in range(40))
    table = f"<table><tr><th>Key</th><th>Value</th></tr>\n{rows}</table>"
    text = (
        " ".join(f"Intro sentence {i:04d} sets up the table." for i in range(30))
        + "\n\n" + table + "\n\n"
        + " ".join(f"Outro sentence {i:04d} follows the table." for i in range(30))
    )
    chunker = PDFChunker(chunk_chars=300, overlap_percent=0.3)
    spans = _pdf_spans(chunker, text)
    assert len(spans) >= 3, "fixture must force a table-crossing boundary"
    assert_adjacent_span_text_contract(spans, writer="pdf_chunker.PDFChunker+table")


def test_pdf_chunker_known_bad_span_claiming_false_abut_is_flagged() -> None:
    """Non-vacuity: corrupt a REAL overlapping pair's second span so it no
    longer claims the overlap (start pushed to prev_end), leaving the text
    untouched — the writer-side shape of nexus-yz7se's original bug. The
    checker must flag it."""
    text = " ".join(f"Sentence number {i:04d} carries its own content." for i in range(400))
    chunker = PDFChunker(chunk_chars=400, overlap_percent=0.3)
    spans = _pdf_spans(chunker, text)
    overlapping = next(
        i for i in range(1, len(spans))
        if spans[i - 1][1] is not None and spans[i][1] is not None
        and spans[i - 1][1] < spans[i][1] < spans[i - 1][2]
    )
    bad = list(spans)
    prev_text, prev_start, prev_end = bad[overlapping - 1]
    cur_text, _cur_start, cur_end = bad[overlapping]
    bad[overlapping] = (cur_text, prev_end, cur_end)  # claim abut; text still duplicates
    violations = check_adjacent_span_text_contract(bad, writer="pdf_chunker.PDFChunker")
    assert violations, "corrupted (false-abut) fixture must be flagged, was not — checker is vacuous"


def test_pdf_chunker_known_bad_span_claiming_false_overlap_is_flagged() -> None:
    """Non-vacuity, the other direction: corrupt a REAL abutting pair's
    second span so it FALSELY claims an overlap the text does not confirm."""
    text = " ".join(f"Sentence number {i:04d} carries its own content." for i in range(400))
    chunker = PDFChunker(chunk_chars=400, overlap_percent=0.3)
    spans = _pdf_spans(chunker, text)
    # Any table-free fixture's LAST pair with genuine non-overlap; here every
    # pair overlaps by construction, so synthesize an abut pair by cross-
    # pairing two chunks that are far apart in the document (no shared text).
    far_a, far_b = spans[0], spans[-1]
    bad_pair: list[Span] = [
        (far_a[0], 0, len(far_a[0])),
        (far_b[0], 5, len(far_b[0]) + 5),  # false overlap claim: 0 < 5 < len(far_a[0])
    ]
    violations = check_adjacent_span_text_contract(bad_pair, writer="pdf_chunker.PDFChunker")
    assert violations, "corrupted (false-overlap) fixture must be flagged, was not — checker is vacuous"


# ── md_chunker.SemanticMarkdownChunker ───────────────────────────────────────


def _md_spans(text: str) -> list[Span]:
    chunks = SemanticMarkdownChunker().chunk(text, {})
    return [
        (c.text, c.metadata.get("chunk_start_char"), c.metadata.get("chunk_end_char"))
        for c in chunks
    ]


def test_md_chunker_multi_section_adjacent_spans_agree_with_text() -> None:
    """Ordinary section-to-section boundaries (``_chunk_sections``'
    non-oversized path): sections are contiguous and abut; no overlap is
    ever claimed between them."""
    text = "\n\n".join(
        f"## Section {i}\n\nParagraph body for section {i}, distinct content only here."
        for i in range(8)
    )
    spans = _md_spans(text)
    assert len(spans) >= 8
    assert_adjacent_span_text_contract(spans, writer="md_chunker.SemanticMarkdownChunker")


def test_md_chunker_oversized_section_adjacent_spans_agree_with_text() -> None:
    """Forces ``_split_large_section``'s overlap path (nexus-yz7se): one
    section far larger than chunk_size, split into several sub-chunks that
    each carry a genuine overlap tail and a re-injected section heading."""
    paragraphs = "\n\n".join(
        f"Paragraph {i:04d} discusses distinct topic content padded out long "
        f"enough that several of these together force a section split here."
        for i in range(40)
    )
    text = f"## Body\n\n{paragraphs}"
    spans = _md_spans(text)
    assert len(spans) >= 3, "fixture must force _split_large_section's overlap branch"
    assert_adjacent_span_text_contract(
        spans, writer="md_chunker.SemanticMarkdownChunker[oversized]", strip_heading=True,
    )


def test_md_chunker_known_bad_span_is_flagged() -> None:
    """Non-vacuity: this is the EXACT shape nexus-yz7se fixed — spans
    recorded as abutting while the text at the seam genuinely duplicates.
    Reproduce it by taking a real oversized-section pair and reporting the
    OLD (pre-fix) span: the previous chunk's own end instead of the
    backed-up start."""
    paragraphs = "\n\n".join(
        f"Paragraph {i:04d} discusses distinct topic content padded out long "
        f"enough that several of these together force a section split here."
        for i in range(40)
    )
    text = f"## Body\n\n{paragraphs}"
    spans = _md_spans(text)
    overlapping = next(
        i for i in range(1, len(spans))
        if spans[i - 1][1] is not None and spans[i][1] is not None
        and spans[i - 1][1] < spans[i][1] < spans[i - 1][2]
    )
    bad = list(spans)
    prev_text, prev_start, prev_end = bad[overlapping - 1]
    cur_text, _cur_start, cur_end = bad[overlapping]
    bad[overlapping] = (cur_text, prev_end, cur_end)  # pre-yz7se: report abut, not backed-up start
    violations = check_adjacent_span_text_contract(
        bad, writer="md_chunker.SemanticMarkdownChunker[oversized]", strip_heading=True,
    )
    assert violations, "yz7se-shaped corruption must be flagged, was not — checker is vacuous"


# ── prose_indexer (nexus.chunker._line_chunk, non-markdown prose path) ──────


def _prose_spans(content: str, *, chunk_lines: int, overlap: float) -> list[Span]:
    raw = _line_chunk(content, chunk_lines=chunk_lines, overlap=overlap)
    offsets = _line_offsets(content)
    spans: list[Span] = []
    for ls, le, text in raw:
        start, end = _char_span_for_lines(content, offsets, ls, le)
        spans.append((text, start, end))
    return spans


def test_prose_indexer_line_chunk_adjacent_spans_agree_with_text() -> None:
    content = "\n".join(f"prose line {i:04d} holds unique wording for this test." for i in range(80))
    spans = _prose_spans(content, chunk_lines=10, overlap=0.3)
    assert len(spans) >= 3, "fixture must force real line-window overlap"
    assert_adjacent_span_text_contract(spans, writer="prose_indexer._line_chunk")


def test_prose_indexer_known_bad_span_is_flagged() -> None:
    content = "\n".join(f"prose line {i:04d} holds unique wording for this test." for i in range(80))
    spans = _prose_spans(content, chunk_lines=10, overlap=0.3)
    overlapping = next(
        i for i in range(1, len(spans))
        if spans[i - 1][1] is not None and spans[i][1] is not None
        and spans[i - 1][1] < spans[i][1] < spans[i - 1][2]
    )
    bad = list(spans)
    prev_text, prev_start, prev_end = bad[overlapping - 1]
    cur_text, _cur_start, cur_end = bad[overlapping]
    bad[overlapping] = (cur_text, prev_end, cur_end)
    violations = check_adjacent_span_text_contract(bad, writer="prose_indexer._line_chunk")
    assert violations, "corrupted fixture must be flagged, was not — checker is vacuous"


# ── code_indexer (nexus.chunker.chunk_file, line-fallback path) ─────────────


def _code_spans(content: str, *, chunk_lines: int) -> list[Span]:
    # An unrecognised extension forces chunk_file's line-based fallback,
    # exercised deterministically without a tree-sitter grammar dependency.
    chunks = chunk_file(Path("fixture.nolang"), content, chunk_lines=chunk_lines)
    offsets = _line_offsets(content)
    spans: list[Span] = []
    for c in chunks:
        start, end = _char_span_for_lines(content, offsets, c["line_start"], c["line_end"])
        spans.append((c["text"], start, end))
    return spans


def test_code_indexer_line_fallback_adjacent_spans_agree_with_text() -> None:
    # Every line kept above chunk_floor.MIN_CHUNK_CHARS (64) so no chunk
    # is short enough to trigger a merge, which is out of this contract's
    # scope (chunk_floor.plan_merges is policy over already-agreeing spans).
    content = "\n".join(
        f"result_{i:04d} = compute_value(input_{i:04d}, factor={i}, padding_for_length=True)"
        for i in range(80)
    )
    spans = _code_spans(content, chunk_lines=10)
    assert len(spans) >= 3, "fixture must force real line-window overlap"
    assert_adjacent_span_text_contract(spans, writer="code_indexer.chunk_file[line-fallback]")


def test_code_indexer_known_bad_span_is_flagged() -> None:
    content = "\n".join(
        f"result_{i:04d} = compute_value(input_{i:04d}, factor={i}, padding_for_length=True)"
        for i in range(80)
    )
    spans = _code_spans(content, chunk_lines=10)
    overlapping = next(
        i for i in range(1, len(spans))
        if spans[i - 1][1] is not None and spans[i][1] is not None
        and spans[i - 1][1] < spans[i][1] < spans[i - 1][2]
    )
    bad = list(spans)
    prev_text, prev_start, prev_end = bad[overlapping - 1]
    cur_text, _cur_start, cur_end = bad[overlapping]
    bad[overlapping] = (cur_text, prev_end, cur_end)
    violations = check_adjacent_span_text_contract(bad, writer="code_indexer.chunk_file[line-fallback]")
    assert violations, "corrupted fixture must be flagged, was not — checker is vacuous"


# ── store_put note_pieces (excluded by construction, pinned here) ───────────


def test_note_pieces_windowed_split_has_no_overlap_claim() -> None:
    """note_pieces' windowed-split regime (nexus-spujb/b2tld): pieces are
    non-overlapping by construction (``"".join(pieces) == content``), so
    every adjacent pair must abut — this pins that store_put's own writer
    already satisfies the contract's abut side, the regime
    join_manifest_parts's docstring calls out as needing the "start must
    ADVANCE" exclusion rather than a naive span comparison."""
    from nexus.catalog.store_hook import note_pieces

    content = " ".join(f"note sentence {i:04d} carries unique wording." for i in range(200))
    pieces = note_pieces(content, "docs__test__voyage-context-3__v1")
    assert len(pieces) >= 2, "fixture must force a real split"
    offset = 0
    spans: list[Span] = []
    for piece in pieces:
        spans.append((piece, offset, offset + len(piece)))
        offset += len(piece)
    assert_adjacent_span_text_contract(spans, writer="store_hook.note_pieces")


# ── nexus-yu16e: manifest POSITION order vs SPAN order ──────────────────────
#
# Measured on a live document (rdr__1-1, the RDR-159 tumbler): two chunks
# adjacent BY MANIFEST POSITION carried spans that ran ~15KB backward.
# RDR-108 defines position as "0-indexed ordinal within the doc" — i.e.
# document sequence — so position disagreeing with span order is a real
# defect if it can happen. A prior sweep of 535 live rdr__/docs__
# documents found zero backward jumps elsewhere. This is the code-level
# half of the re-check the bead asked for: every write site (pipeline_
# stages.py, doc_indexer.py) assigns manifest position from the chunk
# LIST's own enumeration order or its own recorded ``chunk_index`` field
# (never batch-arrival order — see manifest_write_batch_hook's docstring),
# so the question reduces to whether a chunker's own OUTPUT LIST can ever
# be out of span order. These reuse the same fixtures and helpers as the
# span/text contract above; a chunker whose emission order is already
# span-monotonic could not produce the measured anomaly today, which
# supports the "stale data" reading of nexus-yu16e over a live defect.


def _assert_positions_non_decreasing(chunks: list[Span], *, writer: str) -> None:
    violations = [
        f"[{writer}] position {i}->{i + 1}: span start went from "
        f"{chunks[i][1]} to {chunks[i + 1][1]} (backward)"
        for i in range(len(chunks) - 1)
        if chunks[i][1] is not None and chunks[i + 1][1] is not None
        and chunks[i + 1][1] < chunks[i][1]
    ]
    assert not violations, "\n".join(violations)


def test_pdf_chunker_position_order_matches_span_order() -> None:
    text = " ".join(f"Sentence number {i:04d} carries its own content." for i in range(400))
    chunker = PDFChunker(chunk_chars=400, overlap_percent=0.3)
    spans = _pdf_spans(chunker, text)
    assert len(spans) >= 3
    _assert_positions_non_decreasing(spans, writer="pdf_chunker.PDFChunker")


def test_pdf_chunker_with_table_position_order_matches_span_order() -> None:
    rows = "\n".join(f"<tr><td>row-{i:04d}</td><td>value-{i:04d}</td></tr>" for i in range(40))
    table = f"<table><tr><th>Key</th><th>Value</th></tr>\n{rows}</table>"
    text = (
        " ".join(f"Intro sentence {i:04d} sets up the table." for i in range(30))
        + "\n\n" + table + "\n\n"
        + " ".join(f"Outro sentence {i:04d} follows the table." for i in range(30))
    )
    chunker = PDFChunker(chunk_chars=300, overlap_percent=0.3)
    spans = _pdf_spans(chunker, text)
    assert len(spans) >= 3
    _assert_positions_non_decreasing(spans, writer="pdf_chunker.PDFChunker+table")


def test_md_chunker_multi_section_position_order_matches_span_order() -> None:
    text = "\n\n".join(
        f"## Section {i}\n\nParagraph body for section {i}, distinct content only here."
        for i in range(8)
    )
    spans = _md_spans(text)
    assert len(spans) >= 8
    _assert_positions_non_decreasing(spans, writer="md_chunker.SemanticMarkdownChunker")


def test_md_chunker_oversized_section_position_order_matches_span_order() -> None:
    """Forces _split_large_section's overlap-tail path — the SAME writer
    site nexus-yz7se's fix touched, and the most direct code-level probe
    of whether that fix (or an adjacent regression) could reorder a
    section's own sub-chunks."""
    paragraphs = "\n\n".join(
        f"Paragraph {i:04d} discusses distinct topic content padded out long "
        f"enough that several of these together force a section split here."
        for i in range(40)
    )
    text = f"## Body\n\n{paragraphs}"
    spans = _md_spans(text)
    assert len(spans) >= 3, "fixture must force _split_large_section's overlap branch"
    _assert_positions_non_decreasing(
        spans, writer="md_chunker.SemanticMarkdownChunker[oversized]",
    )


def test_prose_indexer_position_order_matches_span_order() -> None:
    content = "\n".join(f"prose line {i:04d} holds unique wording for this test." for i in range(80))
    spans = _prose_spans(content, chunk_lines=10, overlap=0.3)
    assert len(spans) >= 3
    _assert_positions_non_decreasing(spans, writer="prose_indexer._line_chunk")


def test_code_indexer_position_order_matches_span_order() -> None:
    content = "\n".join(
        f"result_{i:04d} = compute_value(input_{i:04d}, factor={i}, padding_for_length=True)"
        for i in range(80)
    )
    spans = _code_spans(content, chunk_lines=10)
    assert len(spans) >= 3
    _assert_positions_non_decreasing(spans, writer="code_indexer.chunk_file[line-fallback]")


def test_position_order_check_is_not_vacuous_on_a_reordered_fixture() -> None:
    """Non-vacuity: two spans swapped out of order must be flagged — the
    exact shape of the live anomaly (adjacent-by-position, ~15KB apart)."""
    text = " ".join(f"Sentence number {i:04d} carries its own content." for i in range(400))
    chunker = PDFChunker(chunk_chars=400, overlap_percent=0.3)
    spans = _pdf_spans(chunker, text)
    assert len(spans) >= 3
    bad = list(spans)
    bad[0], bad[1] = bad[1], bad[0]
    with pytest.raises(AssertionError):
        _assert_positions_non_decreasing(bad, writer="pdf_chunker.PDFChunker")
