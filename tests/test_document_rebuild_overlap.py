# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-kas9u: the whole-document rebuild must drop the chunker's overlap.

``split_note_text`` was written for ``store_put`` notes, whose pieces are
non-overlapping, so it joined manifest parts with ``"".join``. Applied to a
PDF document, whose chunks overlap by ``_DEFAULT_OVERLAP`` of the window by
design, that reproduces the overlap: measured on the KnowFeat paper
(tumbler 1.12.152, 57 chunks), 59 duplicated runs covering 14,199 of 75,999
characters, the longest exactly ``overlap_chars``. Individual chunk reads
were clean throughout; only the rebuild was affected, on both the MCP
``store_get`` and the ``nx store get`` path.

The join trims only, and only when the recorded spans say two parts overlap
AND the text confirms it. Three shapes must NOT be trimmed: the post-pass
sub-pieces of one window (``pdf_chunker`` copies metadata wholesale, so they
share a span), parts with no recorded span at all (``store_put`` notes and
pre-span chunks), and a table continuation whose header prefix means the
next part does not begin with the previous part's tail.
"""
from __future__ import annotations

import re

from nexus.catalog.store_hook import join_manifest_parts
from nexus.pdf_chunker import PDFChunker

# Distinctive, non-repeating prose: any span repeated in the rebuild came
# from the join, not from the source saying the same thing twice.
SOURCE = " ".join(
    f"Paragraph {i:03d} discusses an unrepeated matter of substance {i * 7:04d}."
    for i in range(80)
)


def _parts_from_chunker(text: str, chunk_chars: int = 400) -> list[tuple[str, int, int]]:
    """The (text, start, end) triples the rebuild sees for a real chunking."""
    chunks = PDFChunker(chunk_chars=chunk_chars).chunk(text, {})
    return [
        (c.text, c.metadata["chunk_start_char"], c.metadata["chunk_end_char"])
        for c in chunks
    ]


def _longest_repeated_span(text: str, window: int = 60) -> str:
    seen: set[str] = set()
    for i in range(len(text) - window):
        span = text[i : i + window]
        if span in seen:
            return span
        seen.add(span)
    return ""


def test_the_chunker_really_does_overlap() -> None:
    """Non-vacuity: if the chunker stopped overlapping, every assertion below
    would pass against a join that does nothing, and this file would be
    testing nothing at all."""
    parts = _parts_from_chunker(SOURCE)
    assert len(parts) > 3, "fixture must produce several chunks"
    overlapping = [
        (prev, cur)
        for (_, _, prev_end), (_, cur_start, _) in zip(parts, parts[1:], strict=False)
        if cur_start < prev_end
        for prev, cur in [(prev_end, cur_start)]
    ]
    assert overlapping, "fixture must produce at least one overlapping pair"
    assert "".join(p[0] for p in parts) != SOURCE, (
        "the naive join must still be wrong, or this bead is already fixed "
        "somewhere else and this test no longer pins it"
    )


def test_rebuild_drops_the_overlap_and_repeats_nothing() -> None:
    parts = _parts_from_chunker(SOURCE)
    rebuilt = join_manifest_parts(parts)

    repeated = _longest_repeated_span(rebuilt)
    assert repeated == "", f"rebuild repeated a 60-char span: {repeated!r}"
    for i in (0, 17, 40, 79):
        needle = f"Paragraph {i:03d} discusses"
        assert rebuilt.count(needle) == 1, f"{needle!r} appears {rebuilt.count(needle)} times"


def test_rebuild_invents_no_token_the_source_does_not_have() -> None:
    """A splice invents tokens like the measured "outflows.Indicator" and
    "L2ach", which appear nowhere in the source."""
    rebuilt = join_manifest_parts(_parts_from_chunker(SOURCE))
    source_words = set(re.findall(r"[A-Za-z]+", SOURCE))
    invented = {w for w in re.findall(r"[A-Za-z]+", rebuilt) if w not in source_words}
    assert invented == set(), f"rebuild invented tokens: {sorted(invented)[:5]}"


# A source of single long words, so a fixed-width window cannot avoid
# breaking one. Verified below rather than assumed.
UNBROKEN = " ".join(f"tokenoflength{i:02d}aaaaaaaaaaaaaaaaaaaaaaaaaaaa" for i in range(40))


def test_rebuild_heals_a_boundary_that_fell_mid_word() -> None:
    """The splice half of the defect. A chunk may end mid-word; the next
    chunk starts before that point, so dropping the overlap restores the
    word. The KnowFeat rebuild read "10: L2ach dimension" where the chunk
    itself held "10: L2: Check IV, KS, redundancy"."""
    parts = _parts_from_chunker(UNBROKEN, chunk_chars=300)
    mid_word = [
        prev_text
        for (prev_text, _, prev_end), (_, cur_start, _) in zip(parts, parts[1:], strict=False)
        if cur_start < prev_end and prev_text and prev_text[-1].isalnum()
    ]
    assert mid_word, "fixture must break at least one word, or this pins nothing"

    rebuilt = join_manifest_parts(parts)
    for i in (0, 13, 39):
        whole = f"tokenoflength{i:02d}aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert rebuilt.count(whole) == 1, f"{whole!r} appears {rebuilt.count(whole)} times"


def test_sub_pieces_of_one_window_are_joined_untouched() -> None:
    """pdf_chunker's byte/token post-pass splits an oversized chunk into
    pieces that all carry a copy of the parent's metadata, so their spans
    are identical rather than overlapping. They rejoin with no separator and
    nothing trimmed; treating the shared span as an overlap would delete
    most of the document."""
    parts = [("First half of one window. ", 100, 400), ("Second half of it.", 100, 400)]
    assert join_manifest_parts(parts) == "First half of one window. Second half of it."


def test_parts_with_no_recorded_span_join_exactly_as_before() -> None:
    """store_put notes and any pre-span chunk: no span, no trim, byte-exact
    concatenation. tests/test_store_put_split.py asserts note_pieces rejoin
    to the original with "".join, and that contract is unchanged."""
    parts = [("Sentence one. ", None, None), ("Sentence two.", None, None)]
    assert join_manifest_parts(parts) == "Sentence one. Sentence two."


def test_adjacent_parts_are_not_trimmed() -> None:
    """The chunker starts the next chunk exactly at ``end`` around a table,
    with no overlap. Nothing to drop."""
    parts = [("prose before a table:", 0, 50), ("<table><tr><td>a</td></tr>", 50, 90)]
    assert join_manifest_parts(parts) == "prose before a table:<table><tr><td>a</td></tr>"


def test_an_unconfirmed_overlap_is_left_whole() -> None:
    """A table continuation is prefixed with the table's header row, so it
    does not begin with the previous part's tail even though the spans
    overlap. Trimming on the span alone would eat real rows, so the span
    only ever proposes and the text decides."""
    parts = [
        ("<table><tr><td>h</td></tr><tr><td>1</td></tr>", 0, 100),
        ("<table><tr><td>h</td></tr><tr><td>2</td></tr>", 80, 180),
    ]
    joined = join_manifest_parts(parts)
    assert joined.count("<tr><td>2</td></tr>") == 1
    assert joined.count("<tr><td>1</td></tr>") == 1
