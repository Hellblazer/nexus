# SPDX-License-Identifier: AGPL-3.0-or-later
"""The author-alone read of MCP ``catalog_search`` (RDR-241 Phase 2, nexus-uwggq.2.12).

One bounded request per page to the engine's author route through ``documents_by_author``, never a walk of the
catalog. The route caps ``limit`` at 1000, so a caller that wants more rows gets the route paged here."""
from __future__ import annotations

from typing import Any

from nexus.catalog.engine_reads import by_author
from nexus.catalog.engine_reads.route_unavailable import DOC_PAGE_MAX, EngineRouteUnavailable


def read_author_rows(cat: Any, author: str, *, want: int, offset: int) -> tuple[list[Any], int]:
    """Up to *want* raw rows from *offset* for *author*, and the route's exact match count.

    Pages the route in steps of at most ``DOC_PAGE_MAX`` until *want* rows are held or the matches run out. Raises
    :class:`EngineRouteUnavailable` when the engine, or a reader that has no author route, does not answer; a
    failure on any page fails the whole read, so a caller never sees a partial page."""
    read = getattr(cat, "documents_by_author", None)
    if read is None:
        raise EngineRouteUnavailable(by_author.ROUTE, by_author.FLAG, EngineRouteUnavailable.UNSUPPORTED,
                                     "this catalog reader has no author route")
    rows: list[Any] = []
    count = 0
    while len(rows) < want:
        page_limit = min(DOC_PAGE_MAX, want - len(rows))
        page, count = read(author, limit=page_limit, offset=offset + len(rows))
        rows.extend(page)
        if len(page) < page_limit or offset + len(rows) >= count:
            break
    return rows, count
