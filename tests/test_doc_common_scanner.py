# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ioz2 (code-review-expert on nx catalog footnotes / GH #896):
the shared markdown scanner (:mod:`nexus.doc._common`) must skip inline
backtick code spans and 4-space-indented code blocks, not just triple-
fenced blocks — a docs page explaining
``[label](nx://catalog/<tumbler>)`` syntax almost certainly quotes it
inside inline backticks, and this repo's own ``docs/cli-reference.md``
does exactly that.
"""
from __future__ import annotations

from nexus.doc._common import iter_plain_lines
from nexus.doc.catalog_links import scan_catalog_links


class TestInlineCodeSpanMasking:
    def test_single_backtick_span_is_masked(self) -> None:
        line = "See `[x](nx://catalog/1.1.1)` as an example."
        (lineno, masked), = list(iter_plain_lines(line))
        assert lineno == 1
        assert len(masked) == len(line)  # length-preserving
        assert "nx://catalog" not in masked
        assert "See " in masked  # real prose either side untouched
        assert " as an example." in masked

    def test_double_backtick_span_wrapping_a_single_backtick_is_masked(self) -> None:
        # CommonMark: `` `x` `` uses a longer delimiter to wrap content
        # that itself contains a backtick.
        line = "Use `` `nx://catalog/1.1.1` `` literally."
        (_, masked), = list(iter_plain_lines(line))
        assert "nx://catalog" not in masked
        assert "Use " in masked and " literally." in masked

    def test_scan_catalog_links_ignores_link_inside_inline_code_span(self) -> None:
        text = "Example syntax: `[tumbler 1.1.1](nx://catalog/1.1.1)` is the shape.\n"
        assert scan_catalog_links(text) == []

    def test_scan_catalog_links_still_finds_a_real_link_after_an_inline_example(self) -> None:
        text = (
            "Example syntax: `[x](nx://catalog/9.9.9)` shows the shape.\n"
            "Real citation: [tumbler 1.1.2](nx://catalog/1.1.2) here.\n"
        )
        links = scan_catalog_links(text)
        assert [link.tumbler for link in links] == ["1.1.2"]

    def test_masking_preserves_column_positions_for_a_later_real_match(self) -> None:
        text = "`code` then [x](nx://catalog/1.1.1)\n"
        links = scan_catalog_links(text)
        assert len(links) == 1
        link = links[0]
        # The column reported must index into the ORIGINAL text exactly
        # at the real "[" -- not shifted by the masked prefix.
        assert text[link.col - 1] == "["
        assert text[link.col - 1:].startswith(f"[{link.display}](nx://catalog/{link.tumbler})")


class TestIndentedCodeBlockSkipped:
    def test_indented_block_after_blank_line_is_skipped(self) -> None:
        text = (
            "prose line\n"
            "\n"
            "    [example](nx://catalog/1.1.1)\n"
            "\n"
            "more prose\n"
        )
        links = scan_catalog_links(text)
        assert links == []

    def test_indented_block_continues_through_blank_lines(self) -> None:
        text = (
            "\n"
            "    [a](nx://catalog/1.1.1)\n"
            "\n"
            "    [b](nx://catalog/1.1.2)\n"
            "not indented: [c](nx://catalog/1.1.3)\n"
        )
        links = scan_catalog_links(text)
        assert [link.tumbler for link in links] == ["1.1.3"]

    def test_merely_indented_continuation_line_is_not_treated_as_code(self) -> None:
        """An indented line NOT preceded by a blank line is an ordinary
        (lazily-continued) paragraph line in CommonMark, not an indented
        code block -- must still be scanned."""
        text = "first line of a paragraph\n    [x](nx://catalog/1.1.1)\n"
        links = scan_catalog_links(text)
        assert [link.tumbler for link in links] == ["1.1.1"]

    def test_tab_indented_line_is_also_skipped(self) -> None:
        text = "\n\t[example](nx://catalog/1.1.1)\n"
        links = scan_catalog_links(text)
        assert links == []


class TestGh896WorkedExampleInline:
    """The issue's own worked-example 'Before' text, embedded INLINE
    (not inside a triple-fenced block) -- a pipe table row with inline
    code spans must not confuse the scanner into missing or
    duplicating the one real citation on that row."""

    def test_worked_example_table_row_finds_exactly_the_real_link(self) -> None:
        text = (
            "This implements two objectives of Conductor's F26/F27 1H plan\n"
            "([tumbler 1.1.146](nx://catalog/1.1.146)).\n"
            "\n"
            "Geppetto integration spec at [nx catalog 1.1.194](nx://catalog/1.1.194):\n"
            "PromptLayer endpoint shape, streaming protocol, account_id plus feature\n"
            "parameter conventions.\n"
            "\n"
            "| Geppetto PromptLayer endpoint "
            "| [nx catalog 1.1.194](nx://catalog/1.1.194) "
            "| `POST /v2/geppetto/promptlayer/{template_id}?stream=true&account_id=...&feature=...` "
            "returns SSE; events `delta`, `tool_use`, `done`. |\n"
        )
        links = scan_catalog_links(text)
        # Three real citations total (two of 1.1.194 -- the prose mention
        # and the table-row mention -- plus 1.1.146); none of the inline
        # code spans (the POST endpoint, `delta`, `tool_use`, `done`)
        # contain a catalog link and must not appear or be miscounted.
        assert [link.tumbler for link in links] == ["1.1.146", "1.1.194", "1.1.194"]
