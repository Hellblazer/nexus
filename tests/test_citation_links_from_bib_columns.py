# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7mcev: ``generate_citation_links`` reads the bib ids the way enrichment writes them.

nexus-9l2lg moved ``bib_semantic_scholar_id`` and ``bib_openalex_id`` from the catalog row's
``meta`` JSON to catalog columns, and left ``references`` in ``meta``. The generator kept
reading the ids from ``meta`` alone, so a paper enriched since then was never a link target and
``nx enrich bib`` auto-generated no citation link. These tests use stand-in catalog entries (no
storage); the real enrich-then-link path is in
``tests/integration/test_6pfgb21_bib_enrich_by_manifest.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

from nexus.catalog.link_generator import generate_citation_links


class _Cat:
    """Reader and writer in one: ``all_documents`` and ``link_if_absent``."""

    def __init__(self, entries: list[SimpleNamespace]) -> None:
        self._entries = entries
        self.links: list[tuple[str, str, str]] = []

    def all_documents(self) -> list[SimpleNamespace]:
        return self._entries

    def link_if_absent(self, from_t, to_t, link_type, *, created_by) -> bool:
        edge = (str(from_t), str(to_t), link_type)
        if edge in self.links:
            return False
        self.links.append(edge)
        return True


def _paper(tumbler: str, **kw) -> SimpleNamespace:
    return SimpleNamespace(
        tumbler=tumbler, bib_semantic_scholar_id="", bib_openalex_id="", meta={}, **kw,
    )


def test_two_enriched_papers_that_cite_each_other_get_cites_links() -> None:
    """Enrichment writes the id to the column and ``references`` to ``meta``."""
    a = _paper("1.1.1")
    a.bib_semantic_scholar_id, a.meta = "ssA", {"references": ["ssB"]}
    b = _paper("1.1.2")
    b.bib_semantic_scholar_id, b.meta = "ssB", {"references": ["ssA"]}
    cat = _Cat([a, b])

    count = generate_citation_links(cat)

    assert count == 2
    assert sorted(cat.links) == [("1.1.1", "1.1.2", "cites"), ("1.1.2", "1.1.1", "cites")]


def test_an_openalex_column_id_is_a_link_target() -> None:
    a = _paper("1.1.1")
    a.meta = {"references": ["W2"]}
    b = _paper("1.1.2")
    b.bib_openalex_id = "W2"
    cat = _Cat([a, b])

    assert generate_citation_links(cat) == 1
    assert cat.links == [("1.1.1", "1.1.2", "cites")]


def test_a_row_that_predates_the_columns_keeps_its_ids_in_meta() -> None:
    a = _paper("1.1.1")
    a.meta = {"references": ["ssB"]}
    b = _paper("1.1.2")
    b.meta = {"bib_semantic_scholar_id": "ssB"}
    cat = _Cat([a, b])

    assert generate_citation_links(cat) == 1
