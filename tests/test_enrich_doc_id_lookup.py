# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``_build_catalog_doc_id_lookup`` resolves an absolute source path.

``_chroma_source_id_for_entry`` hands the reader the absolute path from a
row's ``file://`` source_uri, while the catalog stores a repo-relative
``file_path``. The path point-query then missed, and ``nx enrich aspects``
skipped the document as "no doc_id mapped" although it was registered:
about 139 of 348 rdr__1-1 documents on 2026-10-27. The lookup now falls
back to the row whose source_uri carries that path, in the same collection.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import nexus.catalog.factory as factory_mod
from nexus.commands.enrich import _build_catalog_doc_id_lookup

COLL = "rdr__1-1__test-embed__v1"
ABS = "/repo/docs/rdr/rdr-127-surface.md"


class _Catalog:
    """Stores file_path relative, source_uri absolute, as the live rows do."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows

    def lookup_doc_id_by_collection_and_path(self, collection: str, path: str) -> str:
        for r in self.rows:
            if r.physical_collection == collection and r.file_path == path:
                return str(r.tumbler)
        return ""

    def find_all(self, _q: str) -> list:
        return []

    def by_source_uri(self, uri: str):
        return next((r for r in self.rows if r.source_uri == uri), None)


def _row(tumbler: str, coll: str = COLL) -> SimpleNamespace:
    return SimpleNamespace(
        tumbler=tumbler, physical_collection=coll,
        file_path="docs/rdr/rdr-127-surface.md", source_uri=f"file://{ABS}",
    )


@pytest.fixture()
def use_catalog(monkeypatch):
    def _install(rows: list[SimpleNamespace]) -> None:
        monkeypatch.setattr(factory_mod, "make_catalog_reader", lambda: _Catalog(rows))
    return _install


def test_absolute_path_resolves_through_source_uri(use_catalog) -> None:
    use_catalog([_row("1.1.1781")])
    lookup = _build_catalog_doc_id_lookup()
    assert lookup is not None
    assert lookup(COLL, ABS) == "1.1.1781"


def test_relative_path_still_resolves(use_catalog) -> None:
    use_catalog([_row("1.1.1781")])
    assert _build_catalog_doc_id_lookup()(COLL, "docs/rdr/rdr-127-surface.md") == "1.1.1781"


def test_source_uri_row_in_another_collection_is_not_taken(use_catalog) -> None:
    use_catalog([_row("1.9.5", coll="docs__1-1__test-embed__v1")])
    assert _build_catalog_doc_id_lookup()(COLL, ABS) == ""
