# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.8: the tags and category an indexed document carries in its catalog meta, as pure rules.

Storage-free; the catalog writes are ``tests/integration/test_6pfgb8_tags_category.py``. The constants
mirror the row writers (``code_indexer``, ``prose_indexer``, ``doc_indexer``, the repo PDF path in
``indexer``), and the source text of each is pinned below so a writer that changes its constant fails
here instead of drifting from the catalog's copy.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.catalog.indexed_meta import document_tags_category, markdown_tags_category, pdf_tags_category

SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"


@pytest.mark.parametrize("content_type,path,expected", [
    ("code", "src/app.py", {"tags": ["py"], "category": "code"}),
    ("code", "src/UPPER.PY", {"tags": ["py"], "category": "code"}),
    ("code", "lib/x.tar.gz", {"tags": ["gz"], "category": "code"}),
    ("code", "Makefile", {"category": "code"}),
    ("code", ".bashrc", {"category": "code"}),
    ("code", "weird.", {"category": "code"}),
    ("prose", "docs/guide.md", {"tags": ["markdown"], "category": "prose"}),
    ("prose", "docs/Guide.MARKDOWN", {"tags": ["markdown"], "category": "prose"}),
    ("prose", "docs/notes.txt", {"tags": ["txt"], "category": "prose"}),
    ("prose", "docs/README", {"category": "prose"}),
    ("rdr", "docs/rdr/rdr-1-x.md", {"tags": ["markdown"], "category": "prose"}),
    ("pdf", "papers/p.pdf", {"tags": ["pdf"], "category": "prose"}),   # the repo path: prose, not paper
])
def test_the_repo_catalog_pass_rules(content_type, path, expected) -> None:
    assert document_tags_category(content_type, path) == expected


@pytest.mark.parametrize("content_type", ["knowledge", "docs", "paper", "", "anything-else"])
def test_a_content_type_the_indexers_do_not_write_gets_no_invented_values(content_type) -> None:
    assert document_tags_category(content_type, "a/b.py") == {}


def test_the_single_file_entry_points_state_what_their_row_writers_state() -> None:
    assert markdown_tags_category() == {"tags": ["markdown"], "category": "prose"}
    assert pdf_tags_category() == {"tags": ["pdf"], "category": "paper"}


def _kwargs(path: str, near: str) -> tuple[str, str]:
    """The ``tags=`` and ``category=`` expressions of the ``make_chunk_metadata`` / dict call that follows *near*."""
    text = (SRC / path).read_text()
    block = text[text.index(near):][:1800]
    tags = re.search(r'tags(?:=|"\s*:\s*)([^,\n]+),', block)
    category = re.search(r'category(?:=|"\s*:\s*)([^,\n]+),', block)
    assert tags and category, (path, near)
    return tags.group(1).strip(), category.group(1).strip()


@pytest.mark.parametrize("path,near,tags,category", [
    ("code_indexer.py", 'content_type="code"', 'ext.lstrip(".")', '"code"'),
    ("prose_indexer.py", 'content_type="markdown"', '"markdown"', '"prose"'),
    ("prose_indexer.py", 'content_type="prose"', 'ext.lstrip(".")', '"prose"'),
    ("doc_indexer.py", 'content_type="markdown"', '"markdown"', '"prose"'),
    ("doc_indexer.py", 'content_type="pdf"', '"pdf"', '"paper"'),
    ("indexer.py", '"tags": "pdf"', '"pdf"', '"prose"'),
])
def test_the_row_writers_still_stamp_the_constants_this_module_mirrors(path, near, tags, category) -> None:
    assert _kwargs(path, near) == (tags, category)
