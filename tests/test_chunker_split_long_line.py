# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for nexus-wuhw: _split_long_line must not produce
mid-identifier fragments when a whitespace boundary is available.

Symptom: in ``code__1-2153__voyage-code-3__v1`` (the ART Java repo),
~24% of micro-chunks started mid-identifier ("tected ...", "ivate ...").
Root cause: the _split_long_line search window for natural breaks was
fixed at the last 20% of the segment, and when no syntactic break
landed there the fallback hard-cut at max_chars sliced through
whatever identifier happened to be at that offset.

The fix adds a tier-2 whitespace search across the full window so any
whitespace boundary is preferred over a mid-token hard cut.
"""
from __future__ import annotations

import re

from nexus.chunker import _split_long_line


_IDENT_CHAR_RE = re.compile(r"[A-Za-z0-9_]")


def _mid_identifier_boundaries(original: str, segments: list[str]) -> list[str]:
    """Return segments whose start position in *original* is preceded
    by an identifier character. A boundary that sits inside a run of
    ``[A-Za-z0-9_]`` is a mid-identifier slice.
    """
    offenders: list[str] = []
    cursor = 0
    for i, seg in enumerate(segments):
        if i == 0:
            cursor += len(seg)
            continue
        # Segments concatenate to the original (lossless invariant).
        prev_char = original[cursor - 1] if cursor > 0 else ""
        first_char = seg[:1]
        if (
            prev_char
            and first_char
            and _IDENT_CHAR_RE.match(prev_char)
            and _IDENT_CHAR_RE.match(first_char)
        ):
            offenders.append(seg[:60])
        cursor += len(seg)
    return offenders


def test_short_line_unchanged() -> None:
    """Below the max_chars threshold returns the line untouched."""
    out = _split_long_line("short line", max_chars=100)
    assert out == ["short line"]


def test_split_prefers_syntactic_break_in_last_20pct() -> None:
    """When a semicolon strictly inside the last 20% window, it wins
    over later whitespace boundaries (existing tier-1 contract)."""
    # max_chars=40. Last-20% window is positions > 32 and < 40.
    # Place the semicolon at position 36 (well inside the window).
    # Layout: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa;morestuff_here..."
    line = ("a" * 36) + ";morestuff and more characters past 40"
    out = _split_long_line(line, max_chars=40)
    assert out[0].endswith(";"), out[0]


def test_split_uses_whitespace_when_no_syntactic_break() -> None:
    """The wuhw fix: when no syntactic break lives in the last 20%,
    fall back to ANY whitespace boundary before hard-cutting.

    Pre-fix: ``protected void doSomething`` at the boundary would be
    hard-cut, leaving ``tected void doSomething`` at the next chunk's
    start.
    """
    # Java-shaped run: identifiers separated by spaces, no semicolons
    # anywhere in the last-20% window before max_chars.
    # max_chars = 50; the cut would land mid-"CanonicalLaminarCircuitGPU"
    # without the whitespace tier.
    line = (
        "protected CanonicalLaminarCircuitGPU(CanonicalParameters params, "
        "OrientingAdapter orienter, AttentionalAdapter attention)"
    )
    out = _split_long_line(line, max_chars=50)
    offenders = _mid_identifier_boundaries(line, out)
    assert not offenders, (
        f"chunk(s) start mid-identifier: {offenders!r}"
    )


def test_split_emits_clean_boundaries_for_java_like_run() -> None:
    """Composite regression mirroring the bead's observed fragments."""
    line = (
        "private void initializeMetalKernels() throws Exception { "
        "float executeIterationBatched(int dimension, int groupingDim) "
        "throws Exception ProcessingResult processGPUIterationLoop"
        "(float[] input) throws Exception if (iter % 10 == 0 && "
        "count >= minStableIterations) return; if (maxL6 >= "
        "minL6Threshold) return;"
    )
    out = _split_long_line(line, max_chars=60)
    offenders = _mid_identifier_boundaries(line, out)
    assert not offenders, (
        f"chunk(s) start mid-identifier: {offenders!r}"
    )
    # Every chunk should fit within the budget (allowing for trailing
    # break char).
    assert all(len(s) <= 60 + 1 for s in out)


def test_split_falls_through_to_hard_cut_when_no_whitespace() -> None:
    """Truly minified single-token input has no whitespace anywhere; the
    splitter must still produce chunks (tier 3 hard cut is acceptable
    here because no boundary preserves tokens)."""
    line = "abcdefghij" * 20  # 200 chars, no whitespace
    out = _split_long_line(line, max_chars=50)
    assert sum(len(s) for s in out) == 200
    assert all(len(s) <= 50 for s in out)


def test_split_full_segment_concatenation_is_lossless() -> None:
    """Concatenation invariant: joining the segments back gives the
    original line (modulo whitespace cut at boundaries; the splitter
    includes the break char so no characters are dropped)."""
    line = (
        "ProcessingResult processGPUIterationLoop(float[] input) "
        "throws Exception { return new ProcessingResult(); }"
    )
    out = _split_long_line(line, max_chars=40)
    assert "".join(out) == line
