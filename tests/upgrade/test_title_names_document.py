# SPDX-License-Identifier: AGPL-3.0-or-later
"""The chunk-title attribution check both legacy-row rungs share (manifest-section-labels, manifest-span-fill).

The shapes are the ones read off gate-xr789 on 2026-11-07 (nexus-l8o33): the ETL import gave a PDF's catalog
document the title of one of its chunks, page suffix included, and no file_path. Before the fix every other page's
chunk title failed the check, which left 111553 legacy rows unfilled as "titled for another document".
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.upgrade_ladder.rungs.manifest_section_labels import _title_names_document


def _doc(title: str = "", file_path: str = "") -> SimpleNamespace:
    return SimpleNamespace(title=title, file_path=file_path)


@pytest.mark.parametrize(("doc_title", "chunk_title"), [
    ("tests/fixtures/bft-to-smr.pdf:page-3", "tests/fixtures/bft-to-smr.pdf:page-11"),
    ("papers/2604.15583.pdf:page-12", "papers/2604.15583.pdf:page-7"),
    ("tests/fixtures/bft-to-smr.pdf:page-3", "tests/fixtures/bft-to-smr.pdf:page-3"),
    ("src/a.py:chunk-0", "src/a.py:chunk-4"),
    ("src/a.py:1-20", "src/a.py:40-60"),
])
def test_a_document_titled_after_one_of_its_chunks_owns_its_other_chunks(doc_title: str, chunk_title: str) -> None:
    assert _title_names_document(chunk_title, _doc(title=doc_title))


@pytest.mark.parametrize(("doc", "chunk_title"), [
    # the export-bundle document: named after a whole collection export, its chunks name many files
    (_doc(title="docs__1-1__voyage-context-3__v1.nxexp"), "docs/mcp-servers.md:chunk-0"),
    # a page suffix on both sides does not make two files one
    (_doc(title="papers/2408.04948.pdf:page-2"), "papers/2604.15583.pdf:page-6"),
    (_doc(file_path="src/a.py", title="a.py"), "other/file.py:1-5"),
    # a name that is only a suffix is empty once stripped, and an empty name never matches
    (_doc(title=":page-3"), ":chunk-3"),
    (_doc(), ":chunk-3"),
])
def test_a_chunk_naming_another_file_is_still_refused(doc: SimpleNamespace, chunk_title: str) -> None:
    assert not _title_names_document(chunk_title, doc)


@pytest.mark.parametrize(("doc", "chunk_title"), [
    (_doc(file_path="src/a.py", title="a.py"), "src/a.py:3-9"),
    (_doc(file_path="src/a.py"), "a.py"),
    (_doc(title="Design notes"), "Design notes"),
    (_doc(title="anything"), ""),
])
def test_the_forms_that_matched_before_still_match(doc: SimpleNamespace, chunk_title: str) -> None:
    assert _title_names_document(chunk_title, doc)


def test_a_bare_file_name_title_still_matches_that_file_in_any_directory() -> None:
    """A known leniency, older than the suffix strip and now reaching suffixed titles too: a document titled with a
    bare file name (no directory) takes a chunk titled for that file name in any directory. The ownership guards
    (one live holder, no tombstoned holder, indexed_at) bound it."""
    assert _title_names_document("b/x.pdf:page-11", _doc(title="x.pdf:page-3"))
    assert _title_names_document("b/x.pdf", _doc(title="x.pdf"))
