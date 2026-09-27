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
        (lineno, masked), = list(iter_plain_lines(line, mask_inline_code=True))
        assert lineno == 1
        assert len(masked) == len(line)  # length-preserving
        assert "nx://catalog" not in masked
        assert "See " in masked  # real prose either side untouched
        assert " as an example." in masked

    def test_double_backtick_span_wrapping_a_single_backtick_is_masked(self) -> None:
        # CommonMark: `` `x` `` uses a longer delimiter to wrap content
        # that itself contains a backtick.
        line = "Use `` `nx://catalog/1.1.1` `` literally."
        (_, masked), = list(iter_plain_lines(line, mask_inline_code=True))
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


class TestListItemContinuationNotTreatedAsCode:
    """nexus-3ioz2 REGRESSION fix: a blank-line-preceded indented line
    that is actually a LIST ITEM's own continuation (a further
    paragraph, a table, a nested bullet) must still be scanned -- the
    naive "blank + indented" heuristic wrongly classified it as an
    indented code block. Three real cases from this repo's own docs,
    reproduced exactly."""

    def test_ordered_list_nested_bullet_shape(self) -> None:
        """docs/exploration/taxonomy-projection-tuning.md:118 -- a
        nested bullet (4-space indent) under a numbered step whose own
        continuation line established a 3-space content indent."""
        text = (
            "3. **Pick the inflection.** Start with p75 as a first-pass threshold.\n"
            "   Re-project with that value. Examine:\n"
            "\n"
            "    - **Too few matches** (novel chunks dominate): lower by 0.05.\n"
            "    - **Too many hub matches**: enable --use-icf before lowering.\n"
            "    - **Ranking feels right**: stop.\n"
        )
        lines = dict(iter_plain_lines(text, skip_indented_code=True))
        assert "Too few matches" in lines[4]
        assert "Too many hub matches" in lines[5]
        assert "Ranking feels right" in lines[6]

    def test_checkbox_item_multi_paragraph_body(self) -> None:
        """docs/rdr/rdr-094-*.md:233-279, 965 -- a checkbox list item's
        own body spans multiple 6-space-indented paragraphs, separated
        by blank lines (an ordinary CommonMark "loose list")."""
        text = (
            "- [x] Some checklist item that spans\n"
            "      multiple lines of its own paragraph.\n"
            "\n"
            "      A SECOND paragraph inside the same\n"
            "      checklist item, separated by a blank line.\n"
            "\n"
            "      A third paragraph too.\n"
            "- [x] A sibling checklist item.\n"
        )
        lines = dict(iter_plain_lines(text, skip_indented_code=True))
        assert "SECOND paragraph" in lines[4]
        assert "third paragraph" in lines[7]
        assert "sibling checklist item" in lines[8]

    def test_nested_bullet_with_table_and_trailing_prose(self) -> None:
        """docs/rdr/rdr-196-*.md:162, 169 -- a nested bullet's content
        includes a markdown TABLE (indented to the item's content
        column) and further prose after it, both blank-line-separated
        from the item's opening paragraph."""
        text = (
            "- Four research records, summarized:\n"
            "  - **R1 (verified)** -- first bullet text here\n"
            "    continues on this indented line.\n"
            "  - **R2 (verified)** -- cost of one dispatch:\n"
            "\n"
            "    | dispatch shape | cost |\n"
            "    | --- | --- |\n"
            "    | a | 1 |\n"
            "\n"
            "    trailing prose after the table.\n"
            "  - **R3** -- another item.\n"
        )
        lines = dict(iter_plain_lines(text, skip_indented_code=True))
        assert "dispatch shape" in lines[6]
        assert "trailing prose after" in lines[10]
        assert "another item" in lines[11]

    def test_list_closes_on_a_lower_indent_non_list_line(self) -> None:
        """Once a line's indent falls below every open list level (and
        it isn't itself a new marker), the list context closes and the
        plain indented-code heuristic resumes."""
        text = (
            "- an item\n"
            "  continuation text\n"
            "\n"
            "not part of the list at all\n"
            "\n"
            "    this IS genuine indented code\n"
        )
        lines = dict(iter_plain_lines(text, skip_indented_code=True))
        assert 6 not in lines  # the genuine code line is correctly skipped
        assert "not part of the list" in lines[4]

    def test_genuine_indented_code_unrelated_to_any_list_is_still_skipped(self) -> None:
        """The fix must not regress the ORIGINAL indented-code
        detection for a block with no list context at all."""
        text = (
            "Some prose.\n"
            "\n"
            "    a genuinely indented code line\n"
            "    another code line\n"
            "\n"
            "more prose\n"
        )
        lines = dict(iter_plain_lines(text, skip_indented_code=True))
        assert 3 not in lines
        assert 4 not in lines
        assert "more prose" in lines[6]


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


class TestInlineCodeMaskingIsOptIn:
    """ref_scanner finds collection names inside backticks, so the
    default must leave inline code spans intact (nexus-3ioz2 regression)."""

    def test_default_leaves_inline_code_spans_intact(self) -> None:
        line = "- `docs__alpha`: 10 chunks"
        (_, plain), = list(iter_plain_lines(line))
        assert plain == line

    def test_ref_scanner_still_sees_a_backticked_collection(self, tmp_path) -> None:
        from nexus.doc.ref_scanner import scan_markdown

        p = tmp_path / "doc.md"
        p.write_text("- `docs__alpha`: 10 chunks\n")
        refs = scan_markdown(p, ["docs"])
        assert [r.collection for r in refs] == ["docs__alpha"]

    def test_ref_scanner_still_sees_a_collection_in_an_indented_paragraph(
        self, tmp_path,
    ) -> None:
        """Indented-code skipping is opt-in too: a 4-space-indented,
        blank-line-preceded paragraph outside any list is still a real
        reference for ref_scanner, as it was before nexus-3ioz2."""
        from nexus.doc.ref_scanner import scan_markdown

        p = tmp_path / "doc.md"
        p.write_text("Intro.\n\n    See docs__legacy: 12 chunks.\n")
        refs = scan_markdown(p, ["docs"])
        assert [r.collection for r in refs] == ["docs__legacy"]

    def test_default_yields_an_indented_line(self) -> None:
        text = "Intro.\n\n    indented line\n"
        assert dict(iter_plain_lines(text))[3] == "    indented line"
