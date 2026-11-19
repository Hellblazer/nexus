# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client reader for ``GET /v1/catalog/docs/by-author`` (RDR-241 Phase 2, nexus-uwggq.2.6).

The route answers the tenant's live documents whose author contains the requested text, one page at a time in the
engine's text order of tumbler, as ``{count, limit, offset, documents}``. The client's rule, applied by its final filter
(``_keep`` in ``mcp/catalog.py``): ``needle.lower() in (author or "").lower()``, Python's Unicode default lowercase (the
full mapping), no normalization (a precomposed ``ö`` and ``o`` plus a combining diaeresis stay different), ``%`` and
``_`` literal, an empty or absent author never matching. The engine folds with the simple mapping (``pg_c_utf8``), reads
the final sigma as the medial one and drops U+0307 on both sides, so its answer is a SUPERSET of that rule: it never
leaves out a row the rule keeps, ``count`` is an upper bound, and the final filter removes the rows it adds (the
dotted-I pairs). Sam 2026-11-19, T2 ``nexus/sam-ruling-rdr241-author-fold-2026-11-19``. A caller must apply the rule
to every page; the route's rows alone are not the answer."""
from __future__ import annotations

from typing import Any

from nexus.catalog.engine_reads.route_unavailable import DOC_PAGE_MAX, read_doc_page

ROUTE: str = "/docs/by-author"
#: The ``GET /version`` flag that says the engine answers the route.
FLAG: str = "catalog_docs_by_author_supported"


def read_docs_by_author(client: Any, author: str, *, limit: int = DOC_PAGE_MAX,
                        offset: int = 0) -> tuple[list[dict], int]:
    """One page of the live documents whose author contains *author* (rule in the module docstring):
    ``(documents, count)``. Raises :class:`~nexus.catalog.engine_reads.route_unavailable.EngineRouteUnavailable`
    when the engine does not answer (no request is sent without the flag), ``ValueError`` for an empty *author*."""
    if not author:
        raise ValueError("author must not be empty")
    return read_doc_page(client, ROUTE, FLAG, limit=limit, offset=offset, author=author)
