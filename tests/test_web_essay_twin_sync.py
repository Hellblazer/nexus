# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""An exploration essay published under web/ says the same thing as its docs/exploration twin (nexus-vvw81).

A web/ page is an essay when its footer names its source as ``docs/exploration/<idea>.md``. The check
compares the text blocks (headings, paragraphs, list items) inside ``<main>`` with the twin's blocks,
markup and inline markdown stripped, so an edit to one copy without the other fails here.
"""
from __future__ import annotations

import difflib
import re
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE_RE = re.compile(r'class="footer">Source: <a href="[^"]*/docs/exploration/([^"/]+\.md)"')
BLOCK_TAGS = frozenset({"h1", "h2", "h3", "p", "li"})


class _MainBlocks(HTMLParser):
    """Collects the text of each block element in <main>, skipping the top nav and the footer."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._in_main = False
        self._skip: list[str] = []
        self._cur: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main":
            self._in_main = True
        if not self._in_main:
            return
        classes = (dict(attrs).get("class") or "").split()
        if self._skip or "topnav" in classes or "footer" in classes:
            self._skip.append(tag)
            return
        if tag in BLOCK_TAGS:
            self._cur = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "main":
            self._in_main = False
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if tag in BLOCK_TAGS and self._cur is not None:
            self.blocks.append(" ".join("".join(self._cur).split()))
            self._cur = None

    def handle_data(self, data: str) -> None:
        if self._cur is not None and not self._skip:
            self._cur.append(data)


def _page_blocks(text: str) -> list[str]:
    parser = _MainBlocks()
    parser.feed(text)
    return parser.blocks


def _inline(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"(?<!\w)[*_](\S[^*_]*?)[*_](?!\w)", r"\1", text)
    return " ".join(text.split())


def _md_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        lines = para.strip().splitlines()
        if not lines:
            continue
        if re.match(r"^\s*(?:[-*]|\d+\.)\s", lines[0]):
            items: list[str] = []
            for line in lines:
                if m := re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)", line):
                    items.append(m.group(1))
                else:
                    items[-1] += " " + line.strip()
            blocks += [_inline(i) for i in items]
        else:
            blocks.append(_inline(re.sub(r"^#+\s*", "", " ".join(lines))))
    return blocks


def _essay_pages() -> list[tuple[Path, Path]]:
    pairs = []
    for page in sorted((REPO / "web").glob("*.html")):
        if m := SOURCE_RE.search(page.read_text(encoding="utf-8")):
            pairs.append((page, REPO / "docs" / "exploration" / m.group(1)))
    return pairs


def test_at_least_one_essay_page_is_checked() -> None:
    assert _essay_pages(), "no web/ page names a docs/exploration source; the sync check examined nothing"


def test_every_essay_page_matches_its_twin() -> None:
    for page, twin in _essay_pages():
        assert twin.is_file(), f"{page.name} names {twin.relative_to(REPO)}, which does not exist"
        want = _md_blocks(twin.read_text(encoding="utf-8"))
        got = _page_blocks(page.read_text(encoding="utf-8"))
        diff = "\n".join(difflib.unified_diff(want, got, str(twin.relative_to(REPO)), f"web/{page.name}", lineterm="", n=0))
        assert want == got, f"web/{page.name} and its twin differ:\n{diff}"


def test_the_check_sees_a_one_word_edit() -> None:
    page, twin = _essay_pages()[0]
    text = page.read_text(encoding="utf-8")
    at = text.index("<p>", text.index("<main>")) + len("<p>")
    edited = text[:at] + "Plainly " + text[at:]
    assert _page_blocks(edited) != _md_blocks(twin.read_text(encoding="utf-8"))
