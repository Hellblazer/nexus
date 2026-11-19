# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client reader for ``GET /v1/catalog/docs/by-author`` (RDR-241 Phase 2, nexus-uwggq.2.6).

The route answers the tenant's live documents whose author contains the requested text, one page at a time in the
engine's text order of tumbler, as ``{count, limit, offset, documents}`` with an exact ``count``. Its matching rule:
Unicode default lowercase on both sides, no normalization (a precomposed ``ö`` and ``o`` plus a combining diaeresis
stay different), ``%`` and ``_`` matched literally, an empty or absent author never matching. The engine lowers
with PostgreSQL's simple case mapping (``pg_c_utf8``), the client's final filter (``_keep`` in ``mcp/catalog.py``)
with Python's ``str.lower`` (the full mapping); where they differ is recorded on nexus-uwggq.2.6."""
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
