# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The rows of ``nx catalog list --meta`` without ``--owner`` (RDR-241 Phase 2, nexus-uwggq.2.14).

The engine's metadata route (``GET /v1/catalog/docs/by-meta``, engine-service 0.1.197), in place of reading every
document of the type and filtering on the client. The route answers one page in the engine's text order of tumbler
with an exact ``count``, and its ``limit`` is at most 1000: a larger ``-n`` is read as pages by offset until the
rows are held or the matches run out. ``--sensitive`` travels as the route's own ``sensitive`` clause, so it has no
cap on the number of sensitive documents. There is no fallback: an engine that cannot answer is an error saying
so, never a walk.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import click

from nexus.catalog.engine_reads.route_unavailable import (
    DOC_PAGE_MAX,
    PHASE2_ROUTES_ENGINE,
    EngineRouteUnavailable,
)

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader
    from nexus.catalog.types import CatalogEntry

NEEDS_ROUTE = (f"nx catalog list --meta needs an engine with GET /v1/catalog/docs/by-meta "
               f"(engine-service {PHASE2_ROUTES_ENGINE} or later)")


def meta_page(
    cat: "CatalogReader", *, content_type: str, meta_wanted: list[tuple[str, str]], sensitive: bool, offset: int, limit: int,
) -> "tuple[list[CatalogEntry], int]":
    """``(the page's entries, the route's count)`` for ``list --meta`` with no ``--owner``.

    The engine applied ``offset`` and ``limit``, so the rows are not sliced again; ``documents_with_meta`` runs over
    them as the final filter, so an engine fault cannot widen the answer. A ``--limit`` below 1 or an ``--offset``
    below 0 is a usage error (the route has no such page). An empty ``--meta`` value is a usage error too.
    """
    from nexus.catalog.meta_filter import documents_with_meta  # noqa: PLC0415 — deferred to avoid CLI startup cost

    asking = getattr(cat, "documents_by_meta", None)
    if not callable(asking):
        raise click.ClickException(NEEDS_ROUTE)
    if limit < 1 or offset < 0:
        raise click.UsageError("--limit must be at least 1 and --offset at least 0 with --meta")
    entries: list[CatalogEntry] = []
    count = 0
    while len(entries) < limit:
        try:
            page, count = asking(content_type=content_type or None, meta=meta_wanted, sensitive=sensitive,
                                 limit=min(DOC_PAGE_MAX, limit - len(entries)), offset=offset + len(entries))
        except EngineRouteUnavailable as exc:
            raise click.ClickException(f"nx catalog list --meta: {exc}") from exc
        except ValueError as exc:
            raise click.UsageError(f"--meta: {exc}") from exc
        entries += page
        if not page or offset + len(entries) >= count:
            break
    return documents_with_meta(entries, meta_wanted), count
