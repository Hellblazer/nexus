# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.7 (RDR-236 Phase 1): a markdown file's frontmatter ``author`` reaches ``catalog_documents.author``.

The chunk rows carry it as ``source_author`` (the last writer's, on a shared chunk); the document's own
home is the catalog column. Before this bead neither markdown register call passed it, so every markdown
document's author was empty, and PDF already did register plus a FILL-ONLY update (never clobber a
non-empty author). The markdown rule mirrors it. Real engine; the vector side is the engine's own.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, documents_by_file_path
from tests.integration.test_rdr223_pdf_journey import _COLLECTION

pytestmark = [pytest.mark.integration]


def _index_md(path: Path, corpus: str) -> int:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_markdown

    return index_markdown(path, corpus=corpus, t3=HttpVectorClient(), collection_name=_COLLECTION)


def _doc(path: Path):
    (row,) = documents_by_file_path(str(path.resolve()))
    return row


def test_frontmatter_author_reaches_the_catalog(tmp_path) -> None:
    md = tmp_path / "6pfgb7-author.md"
    md.write_text("---\ntitle: T\nauthor: Jane Q. Writer\n---\n\n# H\n\nBody of 6pfgb7 one.\n")

    assert _index_md(md, "6pfgb7-author") > 0

    assert _doc(md).author == "Jane Q. Writer"


@pytest.mark.parametrize("front,expected", [
    ('author: "Quoted Name"', "Quoted Name"),
    ("author: [Ann Lee, Bo Chen]", "Ann Lee, Bo Chen"),
    ("author:\n  - Ann Lee\n  - Bo Chen", "Ann Lee, Bo Chen"),
], ids=["quoted", "flow-list", "block-list"])
def test_the_author_forms_the_row_writer_reads_all_reach_the_catalog_as_one_string(tmp_path, front, expected) -> None:
    md = tmp_path / "6pfgb7-forms.md"
    md.write_text(f"---\ntitle: T\n{front}\n---\n\n# H\n\nBody of 6pfgb7 forms.\n")

    assert _index_md(md, "6pfgb7-forms") > 0

    assert _doc(md).author == expected


def test_a_curated_author_survives_a_re_index(tmp_path) -> None:
    """Fill-only: the catalog author is a curated field once set; a re-index never clobbers it."""
    md = tmp_path / "6pfgb7-curated.md"
    md.write_text("---\ntitle: T\nauthor: Frontmatter Author\n---\n\n# H\n\nBody of 6pfgb7 curated.\n")
    assert _index_md(md, "6pfgb7-curated") > 0
    ActiveCatalog().update(_doc(md).tumbler, author="Curated Author")

    md.write_text("---\ntitle: T\nauthor: Frontmatter Author\n---\n\n# H\n\nBody of 6pfgb7 curated, changed.\n")
    assert _index_md(md, "6pfgb7-curated") > 0

    assert _doc(md).author == "Curated Author"


def test_a_row_registered_without_an_author_is_filled_by_a_later_re_index(tmp_path) -> None:
    """The update branch alone: the document exists with an empty author (indexed before the file
    gained one, or by a release that never sent it); the re-index fills it."""
    md = tmp_path / "6pfgb7-late.md"
    md.write_text("---\ntitle: T\n---\n\n# H\n\nBody of 6pfgb7 late.\n")
    assert _index_md(md, "6pfgb7-late") > 0
    assert _doc(md).author == ""

    md.write_text("---\ntitle: T\nauthor: Late Author\n---\n\n# H\n\nBody of 6pfgb7 late, changed.\n")
    assert _index_md(md, "6pfgb7-late") > 0

    assert _doc(md).author == "Late Author"


def test_a_file_with_no_author_leaves_the_column_alone(tmp_path) -> None:
    md = tmp_path / "6pfgb7-none.md"
    md.write_text("---\ntitle: T\n---\n\n# H\n\nBody of 6pfgb7 none.\n")
    assert _index_md(md, "6pfgb7-none") > 0
    ActiveCatalog().update(_doc(md).tumbler, author="Set By Hand")

    md.write_text("---\ntitle: T\n---\n\n# H\n\nBody of 6pfgb7 none, changed.\n")
    assert _index_md(md, "6pfgb7-none") > 0

    assert _doc(md).author == "Set By Hand", "an empty frontmatter author is never written over a value"


def test_the_hook_registers_with_the_author_when_no_pre_flight_did(tmp_path) -> None:
    """The register leg of ``_catalog_markdown_hook`` (a caller that bypasses the public entry points)."""
    from nexus.doc_indexer import _catalog_markdown_hook

    md = tmp_path / "6pfgb7-hook.md"
    md.write_text("---\ntitle: T\nauthor: Hook Author\n---\n\n# H\n\nBody.\n")

    _catalog_markdown_hook(md, _COLLECTION, "prose", "6pfgb7-hook", 1)

    assert _doc(md).author == "Hook Author"


def test_the_pre_flight_registration_carries_the_author_without_the_hook(tmp_path, monkeypatch) -> None:
    """The row is minted by the pre-flight, before extraction and indexing; it must not wait for the
    post-index hook (a run that stops between the two still leaves the document with its author)."""
    monkeypatch.setattr("nexus.doc_indexer._catalog_markdown_hook", lambda *a, **k: None)
    md = tmp_path / "6pfgb7-preflight.md"
    md.write_text("---\ntitle: T\nauthor: Pre Flight\n---\n\n# H\n\nBody of 6pfgb7 preflight.\n")

    assert _index_md(md, "6pfgb7-preflight") > 0

    assert _doc(md).author == "Pre Flight"


def test_a_register_that_resolves_onto_an_existing_row_still_fills_an_empty_author(tmp_path, monkeypatch) -> None:
    """Review L5: the hook's lookups missed, so it registered, and the engine answered with the
    existing row untouched (no ``meta`` merge). The follow-up update carried only ``meta``, so an
    empty catalog author on that row was never filled; it rides the same fill-only rule now."""
    from nexus.doc_indexer import _catalog_markdown_hook

    md = tmp_path / "6pfgb7-resolved.md"
    md.write_text("---\ntitle: T\n---\n\n# H\n\nBody of 6pfgb7 resolved.\n")
    assert _index_md(md, "6pfgb7-resolved") > 0
    assert _doc(md).author == ""
    md.write_text("---\ntitle: T\nauthor: Resolved Author\n---\n\n# H\n\nBody of 6pfgb7 resolved.\n")
    monkeypatch.setattr("nexus.doc_indexer._find_curator_document", lambda *a, **k: None)
    monkeypatch.setattr("nexus.doc_indexer._repo_owner_document_for", lambda *a, **k: None)

    _catalog_markdown_hook(md, _COLLECTION, "prose", "6pfgb7-resolved", 1)

    assert _doc(md).author == "Resolved Author"


def test_a_register_that_resolves_onto_a_curated_row_keeps_its_author(tmp_path, monkeypatch) -> None:
    from nexus.doc_indexer import _catalog_markdown_hook

    md = tmp_path / "6pfgb7-resolved-curated.md"
    md.write_text("---\ntitle: T\nauthor: Frontmatter\n---\n\n# H\n\nBody of 6pfgb7 resolved curated.\n")
    assert _index_md(md, "6pfgb7-resolved-curated") > 0
    ActiveCatalog().update(_doc(md).tumbler, author="Curated Author")
    monkeypatch.setattr("nexus.doc_indexer._find_curator_document", lambda *a, **k: None)
    monkeypatch.setattr("nexus.doc_indexer._repo_owner_document_for", lambda *a, **k: None)

    _catalog_markdown_hook(md, _COLLECTION, "prose", "6pfgb7-resolved-curated", 1)

    assert _doc(md).author == "Curated Author"
