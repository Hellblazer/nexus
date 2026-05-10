# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-frai (RDR-060 invariant): papers-curator owned PDFs must not
land in ``knowledge__knowledge``.

Background. ``knowledge__knowledge`` is the canonical home for
MCP-stored knowledge notes (empty source_uri, content_type=
"knowledge"). A 2026-05-08 audit found one cross-project
mis-register in prod: tumbler 1.653.2 (SOVEREIGN2-Grossberg2019.pdf,
content_type=paper, owner=papers curator) wrote into
``knowledge__knowledge`` instead of into the curator's own
``knowledge__art-grossberg-papers__voyage-context-3__v1`` collection.
A PDF row in ``knowledge__knowledge`` violates the
content_type-vs-collection invariant.

These tests assert that no future write path can re-introduce the
class. The invariant is checked at the catalog-write boundary
(``Catalog.register`` and ``Catalog.update``); enforcing it there
is the narrowest fence that catches every caller (CLI, MCP, hooks,
worker paths).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog


@pytest.fixture()
def papers_owner(tmp_path: Path):
    """Fresh Catalog with a papers-curator owner registered. Returns
    (catalog, owner_tumbler).
    """
    catalog_dir = tmp_path / "catalog"
    Catalog.init(catalog_dir)
    c = Catalog(catalog_dir, catalog_dir / ".catalog.db")
    # Mirror the prod shape: papers is a curator owner (no repo_hash
    # required; register_owner enforces repo_hash only for owner_type
    # == "repo"). source_uri rejection in Catalog.register only fires
    # when the owner has a repo_root anchor; curator owners do not.
    owner = c.register_owner("papers", "curator")
    return c, owner


def _scan_for_paper_rows_in_knowledge_knowledge(cat: Catalog) -> list[tuple]:
    """Read-side invariant check used by tests + the doctor.

    Returns rows ``(tumbler, title)`` for any document where
    ``content_type='paper'`` AND ``physical_collection='knowledge__knowledge'``.
    Empty list = invariant holds.
    """
    return list(cat._db.execute(
        "SELECT tumbler, title FROM documents "
        "WHERE content_type = 'paper' "
        "  AND physical_collection = 'knowledge__knowledge'"
    ).fetchall())


class TestCatalogPapersCuratorIsolation:
    """The catalog must not hold any document where
    content_type='paper' AND physical_collection='knowledge__knowledge'.
    """

    def test_clean_catalog_holds_invariant(self, papers_owner) -> None:
        """A catalog with no papers in knowledge__knowledge passes
        the read-side invariant scan trivially.
        """
        cat, _ = papers_owner
        assert _scan_for_paper_rows_in_knowledge_knowledge(cat) == []

    def test_invariant_scan_catches_misregistration(self, papers_owner) -> None:
        """Direct INSERT of a paper into knowledge__knowledge (the
        2026-05-08 prod state for tumbler 1.653.2) must be detectable
        by the scan. This is the "doctor check" shape: a maintenance
        verb can iterate `cat.all_documents()` looking for the same
        violation and surface it for operator action.

        The test seeds the bad row directly via SQL (epsilon-allow
        to bypass the catalog API) so it asserts the SCAN finds the
        class, not that the API rejects it (separate test below).
        """
        cat, owner = papers_owner
        cat._db.execute(  # epsilon-allow: test fixture seeds an invariant-violating row to exercise the read-side scan
            "INSERT INTO documents "
            "(tumbler, title, author, year, content_type, file_path, "
            " corpus, physical_collection, chunk_count, head_hash, "
            " indexed_at, metadata, source_mtime, alias_of, source_uri) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{owner}.99", "BAD-PaperInKnowledge", "", 2024,
                "paper", "/papers/bad.pdf", "default",
                "knowledge__knowledge", 1, "h0",
                "2026-05-10T00:00:00Z", "{}", 0.0, "",
                "file:///papers/bad.pdf",
            ),
        )
        cat._db.commit()

        violations = _scan_for_paper_rows_in_knowledge_knowledge(cat)
        assert len(violations) == 1
        assert violations[0][1] == "BAD-PaperInKnowledge"

    def test_paper_register_into_curator_collection_is_clean(
        self, papers_owner,
    ) -> None:
        """A paper registered into the curator's own collection
        (``knowledge__art-grossberg-papers__...``) does NOT trigger
        the invariant violation. This is the correct shape; we lock
        the positive case so future refactors don't accidentally
        widen the violation match.
        """
        cat, owner = papers_owner
        cat.register(
            owner=owner,
            title="SOVEREIGN-Grossberg2019",
            content_type="paper",
            file_path="/papers/sovereign-grossberg2019.pdf",
            physical_collection=(
                "knowledge__art-grossberg-papers__voyage-context-3__v1"
            ),
        )
        assert _scan_for_paper_rows_in_knowledge_knowledge(cat) == []

    def test_knowledge_note_in_knowledge_knowledge_is_clean(
        self, papers_owner,
    ) -> None:
        """A knowledge note (content_type='knowledge') registered
        into ``knowledge__knowledge`` is the EXPECTED shape; the
        invariant is content-type-scoped (paper-only), not
        collection-scoped. Lock the positive case so future
        refactors don't widen the match to all content types.
        """
        cat, owner = papers_owner
        cat.register(
            owner=owner,
            title="A knowledge note",
            content_type="knowledge",
            file_path="",  # MCP-stored, no source file
            physical_collection="knowledge__knowledge",
            source_uri="",
        )
        assert _scan_for_paper_rows_in_knowledge_knowledge(cat) == []
