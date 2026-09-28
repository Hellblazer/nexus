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
``docs/cli-reference.md`` does. Both are handled HERE, once, and BOTH
are opt-in keyword arguments of :func:`iter_plain_lines`
(``mask_inline_code``, ``skip_indented_code``): a consumer scanning for
link syntax wants them, a consumer scanning for names (ref_scanner,
citations) does not, since a name in backticks or in an indented
paragraph is still a real reference. When enabled:

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
  further indented-or-blank lines.

nexus-3ioz2 REGRESSION (follow-up review): the first cut of the
indented-code heuristic treated ANY blank-line-preceded indented line
as code, including a line that is actually a LIST ITEM's own
continuation — a further paragraph, a table, or nested content, all
indented to match the item's content column, separated from the
item's first paragraph by a blank line (CommonMark allows blank lines
between a list item's paragraphs; this is an ordinary "loose list").
Real cases this silently dropped from every scan: a nested bullet
under a numbered step whose own continuation line established a
3-space content indent (the nested bullet needs 4, still "just
indented text" to the naive heuristic); a checkbox item's own
multi-paragraph body, each paragraph indented to match `- [x] `'s
6-column content indent; a markdown TABLE inside a nested bullet's
content, indented to match ITS content column. `nx doc render`/
`validate` — sharing this same scanner — silently stopped resolving
citations and tokens inside any of these shapes.

Fixed by tracking OPEN LIST CONTEXT, not just blank-line adjacency:
*list_stack* holds the content-indent (the column where an item's own
text starts, i.e. past the marker and its trailing space) of every
currently-open list level, outermost first. A line's indent closes any
open level whose content-indent it no longer reaches (``indent <
list_stack[-1]``); once closed, deeper levels are gone, but a
shallower or exactly-matching one can still be open. A line that
itself opens a new marker (``- ``, ``* ``, ``+ ``, or ``N.``/``N)``)
pushes a new level and is never code. Any OTHER line whose indent
still reaches the deepest remaining open level is that item's content
— a continuation paragraph, nested prose, a table — and is likewise
never code, regardless of a preceding blank line. Only once indent
falls below every open level (or none is open) does the plain 4-space/
blank-line-preceded heuristic apply. This is still not a full
CommonMark parser (nested code blocks genuinely INSIDE a list item's
own content, which need indent beyond the item's own content column,
are not modeled — real docs essentially never do this, and getting it
wrong there would only fail to skip a rare additional-indent code
block, never wrongly skip real prose, which is the class of bug this
fix exists for) but it resolves every case found in this repo's own
docs by a full non-vacuous corpus diff (see the fix's own commit
message for the walked file list and counts).

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

#: A list-item marker at the very start of the post-indent content:
#: bullet (``-``/``*``/``+``) or ordinal (``N.``/``N)``), followed by
#: whitespace or end-of-line (an empty item). Deliberately excludes a
#: thematic break (``---``, ``***``): those require a SECOND marker
#: character immediately after the first, which fails the
#: whitespace-or-end requirement here.
_LIST_MARKER_RE = re.compile(r"^([-*+]|\d{1,9}[.)])(\s+|$)")


def _mask_inline_code_spans(line: str) -> str:
    """Return *line* with every inline code span replaced by
    ``\\x00`` repeated to the same length — the delimiters included.
    Length- and column-preserving, so a caller's reported positions
    still index correctly into the ORIGINAL (unmasked) line.
    """
    return _INLINE_CODE_SPAN_RE.sub(lambda m: _MASK_CHAR * len(m.group(0)), line)


def _identity(line: str) -> str:
    return line


def _leading_indent_and_rest(line: str) -> tuple[int, str]:
    """``(indent, line-with-that-indent-stripped)``. Indent counts
    columns, not characters — a tab advances to the next multiple of 4,
    same as a 4-space indented code block's own convention."""
    indent = 0
    i = 0
    for ch in line:
        if ch == " ":
            indent += 1
            i += 1
        elif ch == "\t":
            indent += 4 - (indent % 4)
            i += 1
        else:
            break
    return indent, line[i:]


def iter_plain_lines(
    text: str, *, mask_inline_code: bool = False, skip_indented_code: bool = False
) -> Iterator[tuple[int, str]]:
    """Yield ``(1-based lineno, line)`` for every non-fenced line.

    Content inside ```` ``` ```` / ``~~~`` fences is always skipped so
    a tutorial snippet doesn't false-positive.

    The two other "not prose" shapes are OPT-IN, because they are right
    for a scanner of link syntax and wrong for a scanner of names:

    * *mask_inline_code* masks each inline code span (see the module
      docstring) rather than dropping the whole line.
    * *skip_indented_code* also skips a 4-space-indented code block,
      using the list-context rules in the module docstring.

    :mod:`nexus.doc.ref_scanner` finds collection names precisely inside
    backticks (`` `docs__x` ``) and in indented paragraphs, so applying
    either to every consumer silently dropped references it reports on.
    The catalog-link and footnote scanners, which must not match link
    syntax quoted as an example, pass both.

    Line numbers preserve original file positions — callers reporting
    errors can report ``file:line:col`` against the real source; the
    yielded line's length matches the original exactly (masking never
    changes it), so a reported column is always valid against the
    real file too.
    """
    emit = _mask_inline_code_spans if mask_inline_code else _identity
    in_fence = False
    fence_marker: str | None = None
    in_indented_code = False
    prev_line_blank = True  # document start counts as "preceded by blank"
    #: Open list levels' content-indent (column where the item's own
    #: text starts), outermost first. See the module docstring's
    #: "nexus-3ioz2 REGRESSION" section.
    list_stack: list[int] = []
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
        if not skip_indented_code:
            yield lineno, emit(line)
            continue

        stripped = line.strip()
        is_blank = stripped == ""
        if is_blank:
            # A blank line never closes an open list level (CommonMark
            # allows blank lines between/within a list item's own
            # paragraphs — an ordinary "loose list") and never itself
            # starts an indented-code block; it only ends a RUN of
            # already-open indented-code lines, same as before.
            if in_indented_code:
                continue
            prev_line_blank = True
            yield lineno, emit(line)
            continue

        indent, rest = _leading_indent_and_rest(line)

        # Close any list levels this line's indent no longer reaches —
        # deeper levels only; a level whose content-indent this line
        # still meets or exceeds stays open.
        while list_stack and indent < list_stack[-1]:
            list_stack.pop()

        marker_m = _LIST_MARKER_RE.match(rest)
        if marker_m:
            trailing_ws = marker_m.group(2)
            marker_width = (
                len(marker_m.group(0)) if trailing_ws else len(marker_m.group(1)) + 1
            )
            list_stack.append(indent + marker_width)
            in_indented_code = False
            prev_line_blank = False
            yield lineno, emit(line)
            continue

        if list_stack and indent >= list_stack[-1]:
            # This item's own continuation: a further paragraph, a
            # table, nested prose — real content, not code, regardless
            # of a preceding blank line.
            in_indented_code = False
            prev_line_blank = False
            yield lineno, emit(line)
            continue

        # Outside any open list's content zone — the plain heuristic.
        is_indented = line.startswith("    ") or line.startswith("\t")
        if in_indented_code:
            if is_indented:
                prev_line_blank = False
                continue  # still inside the block
            in_indented_code = False
            # falls through: this line starts fresh, ordinary content
        elif is_indented and prev_line_blank:
            in_indented_code = True
            prev_line_blank = False
            continue

        prev_line_blank = False
        yield lineno, emit(line)
