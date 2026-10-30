# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.7: the frontmatter parse the markdown register calls share, author included.

Storage-free; the catalog side (the column, fill-only) is ``tests/integration/test_6pfgb7_markdown_author.py``.
"""
from __future__ import annotations

import pytest

from nexus.doc_indexer import _frontmatter_author, _parse_md_frontmatter


@pytest.mark.parametrize("front,expected", [
    ({"author": "Jane Q. Writer"}, "Jane Q. Writer"),
    ({"author": "  Padded  "}, "Padded"),
    ({"author": ["Ann Lee", "Bo Chen"]}, "Ann Lee, Bo Chen"),
    ({"author": ["Ann Lee", None, " "]}, "Ann Lee"),
    ({"author": ""}, ""),
    ({"author": None}, ""),
    ({"author": 42}, ""),
    ({}, ""),
])
def test_the_catalog_author_of_a_frontmatter_mapping(front, expected) -> None:
    assert _frontmatter_author(front) == expected


def test_title_year_and_author_come_from_one_parse(tmp_path) -> None:
    md = tmp_path / "doc.md"
    md.write_text("---\ntitle: The Title\ncreated: 2026-03-14\nauthor: Jane Q. Writer\n---\n\nBody\n")
    assert _parse_md_frontmatter(md) == ("The Title", 2026, "Jane Q. Writer")


@pytest.mark.parametrize("text", ["Body only\n", "---\ntitle: [unclosed\nauthor: X\n---\n\nBody\n", ""])
def test_no_usable_frontmatter_gives_the_stem_and_no_author(tmp_path, text) -> None:
    md = tmp_path / "plain-note.md"
    md.write_text(text)
    _title, year, author = _parse_md_frontmatter(md)  # the title regex is line-based; the author needs valid YAML
    assert (year, author) == (0, "")
