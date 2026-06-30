# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 2 (Gap 1a) — _VERIFY_TABLES completeness for the catalog.

Post-migration count-verify maps source ``(store, table)`` → PG relation in
``_VERIFY_TABLES``. The catalog ETL copies five tables (owners, documents,
collections, document_chunks, links) but ``_VERIFY_TABLES`` mapped only
``documents`` + ``links`` — so a partial copy of owners, collections, or
document_chunks reconciled GREEN ("verified") because those relations were
never checked. The acceptance bar (RDR Decisions) is parity on EVERY catalog
table.

Failing-first (bead nexus-t9rmg.11): a report whose catalog landed fewer
owners / collections / document_chunks than it wrote must verify as
``"mismatch"``, not ``"verified"``.
"""
from __future__ import annotations

import pytest

from nexus.migration.orchestrator import verify_counts

# PG relation names for the five catalog tables (mirrors the documents/links
# naming already in _VERIFY_TABLES: nexus.catalog_<table>).
_REL = {
    "owners": "nexus.catalog_owners",
    "documents": "nexus.catalog_documents",
    "collections": "nexus.catalog_collections",
    "document_chunks": "nexus.catalog_document_chunks",
    "links": "nexus.catalog_links",
}


class _FakeCountSource:
    def __init__(self, pg: dict[str, int]) -> None:
        self._pg = pg

    def counts(self, relations: list[str]) -> dict[str, int] | None:
        # Report exactly the requested relations (the real service behaviour);
        # any relation we know about is answered, so a missing check would be a
        # silent gap, not an indeterminate.
        return {r: self._pg[r] for r in relations if r in self._pg}


def _catalog_report(written: dict[str, int]) -> dict:
    return {
        "stores": [
            {
                "store": "catalog",
                "tables": [
                    {"table": t, "read": n, "written": n}
                    for t, n in written.items()
                ],
            }
        ]
    }


_FULL_WRITTEN = {
    "owners": 4, "documents": 8, "collections": 5,
    "document_chunks": 20, "links": 3,
}


def _full_pg() -> dict[str, int]:
    return {_REL[t]: n for t, n in _FULL_WRITTEN.items()}


def test_full_catalog_copy_verifies() -> None:
    status, _ = verify_counts(_catalog_report(_FULL_WRITTEN), _FakeCountSource(_full_pg()))
    assert status == "verified"


@pytest.mark.parametrize("partial_table", ["owners", "collections", "document_chunks"])
def test_partial_catalog_table_copy_is_a_mismatch(partial_table: str) -> None:
    """A short copy of owners/collections/document_chunks must FAIL verify.

    On current code these three relations are absent from _VERIFY_TABLES, so the
    short copy is never checked and verify falsely returns 'verified'."""
    pg = _full_pg()
    pg[_REL[partial_table]] -= 1  # one row failed to land
    status, _ = verify_counts(_catalog_report(_FULL_WRITTEN), _FakeCountSource(pg))
    assert status == "mismatch", (
        f"a partial {partial_table} copy must be a verify mismatch, got {status!r}"
    )
