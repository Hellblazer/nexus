# SPDX-License-Identifier: AGPL-3.0-or-later
"""The wire contract of the RDR-241 Phase 2 paged document route ``GET /v1/catalog/docs/by-scheme``
(nexus-uwggq.1.5), kept out of the over-cap ``http_catalog_client`` module, whose method calls it.

The route answers the tenant's live documents whose ``urlparse(source_uri).scheme`` equals the requested scheme
lowercased, one page at a time in tumbler order: ``{"count", "limit", "offset", "documents"}``, ``count`` being the
exact number of matches whatever the page, ``documents`` the ``/list`` document maps. An engine advertises it with
``catalog_docs_by_scheme_supported`` on ``GET /version``."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

#: The engine's page bound (``CatalogDocReads.MAX_LIMIT``); the reader asks for full pages.
DOCS_BY_SCHEME_PAGE: int = 1000
#: Client timeout for one page: above the engine's 10 s statement bound (``FILLABLE_COUNT_TIMEOUT_MS``), whose
#: cut answers 503, and under the public edge's roughly 30 s cut.
DOCS_BY_SCHEME_TIMEOUT_S: float = 15.0
#: The ``GET /version`` flag that says the engine answers the route.
DOCS_BY_SCHEME_FLAG: str = "catalog_docs_by_scheme_supported"


def read_docs_by_scheme(
    http_get: Callable[..., Any], flags: Mapping[str, Any], scheme: str, *, page: int = DOCS_BY_SCHEME_PAGE,
) -> list[dict] | None:
    """Every live document of *scheme*, read page by page until the route's exact count is reached.

    ``None`` when *flags* does not hold ``catalog_docs_by_scheme_supported`` as a JSON ``true``: no request is sent,
    and nothing walks the catalog in its place (RDR-241). An HTTP error, a cut, or a malformed body raises (a body
    that is not the contract raises ``ValueError``): a partial list is never returned as a whole one.
    """
    if flags.get(DOCS_BY_SCHEME_FLAG) is not True:
        return None
    out: list[dict] = []
    offset = 0
    while True:
        body = http_get(
            "/docs/by-scheme", idempotent=True, timeout=DOCS_BY_SCHEME_TIMEOUT_S,
            scheme=scheme, limit=page, offset=offset,
        )
        if not isinstance(body, dict):
            raise ValueError(f"docs/by-scheme answered a {type(body).__name__}, not an object")
        count, docs = body.get("count"), body.get("documents")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0 or body.get("offset") != offset or not isinstance(docs, list) \
                or not all(isinstance(d, dict) for d in docs) or len(docs) > page:
            raise ValueError(f"docs/by-scheme body is not the contract (keys {sorted(body)}, offset {offset})")
        if not docs and offset < count:
            raise ValueError(f"docs/by-scheme gave an empty page at offset {offset} of a count of {count}")
        out.extend(docs)
        offset += len(docs)
        if offset >= count:
            return out
