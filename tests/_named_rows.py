# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A pytest plugin (registered from ``tests/conftest.py``): the ``named_collection_rows`` fixture.

RDR-229 P2.9 (nexus-p2m1x.14): the routers read a collection's content type from its catalog row
(``nexus.mcp_infra.get_collection_row``), never from its name. A unit test whose fixtures are
prefix-named collections (``knowledge__x``, ``code__repo``) and that builds no engine and no T3 has
no row cache to read; this fixture answers each name with the row a registered collection of that
name would carry, through the suite's one shared emitter (``tests.conftest.catalog_row_for_collection_name``).
The names are test fixtures, so tests/ may parse them (the parse census scans only ``src/nexus``).

Use it as ``pytestmark = pytest.mark.usefixtures("named_collection_rows")`` on a module whose tests
route prefix-named collections. A test that wants a collection with NO row, or a row that disagrees with
its name, patches ``nexus.mcp_infra.get_collection_row`` itself after this fixture (a later patch wins).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def named_collection_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import catalog_row_for_collection_name

    def _get_collection_row(name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
        if not name:
            return None
        row = catalog_row_for_collection_name(name)
        parts = name.split("__")
        if len(parts) == 1 and name not in ("code", "docs", "rdr", "knowledge"):
            row["content_type"] = "knowledge"  # a bare name registers as knowledge (collection_registration_kwargs)
        if len(parts) == 4:
            # a conformant name was registered with the model it names, not its content type's default
            row["embedding_model"] = parts[2]
        return row

    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _get_collection_row)


@pytest.fixture
def rowless_collection_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client row cache answers "no row" for every name and its invalidate is a no-op (RDR-229 P2.9).

    A tenant-free test whose collections hold no chunk takes item 1's NoRow outcome at every router (the
    candidate name's own segment) after one cache invalidate and re-read; with no tenant that re-read would
    list the (absent) service. This fixture is that outcome without the listing, so the test keeps exactly
    the name-derived behaviour it was written against.
    """

    def _no_row(name: str, *, refresh: bool = True, strict: bool = False) -> dict | None:
        return None

    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", _no_row)
    monkeypatch.setattr("nexus.mcp_infra.invalidate_collections_cache", lambda: None)
