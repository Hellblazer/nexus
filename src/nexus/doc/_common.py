# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared helpers for the ``nexus.doc`` module.

Used by :mod:`nexus.doc.ref_scanner` (RDR-081), :mod:`nexus.doc.tokens`
(RDR-082), and :mod:`nexus.doc.catalog_links` / :mod:`nexus.doc.
footnote_converter` (GH #896) — anything that walks markdown and must
respect fenced code blocks lands here.

nexus-3ioz2 review round (code-review-expert on nx catalog footnotes):
a scanner keyed only on triple-fenced blocks misses TWO other markdown
"this is not prose" shapes, and misses them in exactly the files most
likely to demonstrate the syntax being scanned for — a docs page
explaining ``[label](nx://catalog/<tumbler>)`` almost certainly quotes
that literal syntax inside inline backticks, and this very repo's own
``docs/cli-reference.md`` does. Both are handled HERE, once, so every
consumer of :func:`iter_plain_lines` benefits without its own fix:

* **Inline code spans** — `` `...` `` or `` ``...`` `` (CommonMark:
  closing delimiter must be the SAME backtick-run length as the
  opener) — are MASKED in place: every character of the matched span
  (delimiters included) is replaced with ``\\x00``, preserving the
  line's length and column positions exactly. A caller's regex can
  never match inside the masked run (no real markdown syntax character
  survives), while a caller that reconstructs output from the ORIGINAL
  line (indexing by the reported column, never by the masked text
  itself) sees the real content untouched.
* **4-space-indented code blocks** — a CommonMark indented code block:
  a line indented >= 4 columns (or a tab), starting only after a blank
  line (or the document start) and continuing through any number of
  further indented-or-blank lines. This is a HEURISTIC, not a full
  CommonMark implementation (it does not special-case list-item
  continuation indentation), but it is deliberately conservative in
  the direction that matters here: it can suppress the odd equally-
  indented paragraph line, never leak a genuine indented-code line's
  content into a scan.

Single-line only: an inline code span or indented block spanning
multiple markdown constructs each still resolves per physical line, so
a code span is masked only when its opening and closing delimiters
share one line (any run of consecutive lines can share fence state,
but an unclosed inline span on one line does not "leak" masking onto
the next).
"""
from __future__ import annotations

import re
from collections.abc import Iterator


#: Markdown fence opener/closer (triple-backtick or triple-tilde).
#: Matches any leading whitespace so indented fences inside list items
#: are still recognised.
FENCE_RE = re.compile(r"^\s*(```|~~~)")

#: One inline code span: an opening backtick run, the shortest possible
#: content, then a closing run of the SAME length (backreference),
#: rejecting a longer run right after (so a 2-backtick open can't match
#: prematurely against the first single backtick it meets, and a
#: content backtick doesn't falsely close a longer-delimited span).
_INLINE_CODE_SPAN_RE = re.compile(r"(`+)(.*?)\1(?!`)")

#: The masking placeholder. Never a character any markdown-syntax
#: regex in this codebase treats as meaningful, so a masked span can
#: never re-match as a link/token/citation/footnote-marker shape.
_MASK_CHAR = "\x00"


def _mask_inline_code_spans(line: str) -> str:
    """Return *line* with every inline code span replaced by
    ``\\x00`` repeated to the same length — the delimiters included.
    Length- and column-preserving, so a caller's reported positions
    still index correctly into the ORIGINAL (unmasked) line.
    """
    return _INLINE_CODE_SPAN_RE.sub(lambda m: _MASK_CHAR * len(m.group(0)), line)


def iter_plain_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(1-based lineno, line)`` for every non-fenced,
    non-indented-code line, with inline code spans masked out.

    Content inside ```` ``` ```` / ``~~~`` fences, and inside a
    4-space-indented code block, is skipped entirely (yielded to no
    one) so a tutorial snippet or an indented example doesn't
    false-positive. An inline code span WITHIN an otherwise-plain line
    is masked (see the module docstring) rather than dropping the
    whole line, since the rest of that line is still real prose a
    caller should scan.

    Line numbers preserve original file positions — callers reporting
    errors can report ``file:line:col`` against the real source; the
    yielded line's length matches the original exactly (masking never
    changes it), so a reported column is always valid against the
    real file too.
    """
    in_fence = False
    fence_marker: str | None = None
    in_indented_code = False
    prev_line_blank = True  # document start counts as "preceded by blank"
    for lineno, line in enumerate(text.splitlines(), 1):
        m = FENCE_RE.match(line)
        if m:
            if not in_fence:
                in_fence = True
                fence_marker = m.group(1)
            elif m.group(1) == fence_marker:
                in_fence = False
                fence_marker = None
            prev_line_blank = False
            continue
        if in_fence:
            continue

        stripped = line.strip()
        is_blank = stripped == ""
        is_indented = (not is_blank) and (line.startswith("    ") or line.startswith("\t"))

        if in_indented_code:
            if is_blank:
                continue  # a blank line inside the block is still part of it
            if is_indented:
                continue  # still inside the block
            in_indented_code = False
            # falls through: this line starts fresh, ordinary content
        elif is_indented and prev_line_blank:
            in_indented_code = True
            continue

        prev_line_blank = is_blank
        yield lineno, _mask_inline_code_spans(line)
