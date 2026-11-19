# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The rows of ``nx catalog list --meta`` without ``--owner`` (RDR-241 Phase 2, nexus-uwggq.2.14).

The engine's metadata route (``GET /v1/catalog/docs/by-meta``, in the engine release that carries the RDR-241 Phase 2
routes), in place of reading every document of the type and filtering on the client. The route answers one page in
the engine's text order of tumbler with an exact ``count``, and its ``limit`` is at most 1000: a larger ``-n`` is
read as pages by offset until the rows are held or the matches run out. ``--sensitive`` travels as the route's own
``sensitive`` clause, so it has no cap on the number of sensitive documents, and ``nx catalog list --sensitive`` goes
through the same rows when the engine's sensitive list cannot answer (``sensitive_listing``). There is no fallback:
an engine that cannot answer is an error saying so, never a walk.
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


def meta_rows(
    cat: "CatalogReader", *, content_type: str, meta_wanted: list[tuple[str, str]], sensitive: bool, offset: int, limit: int,
) -> "tuple[list[CatalogEntry], int]":
    """``(the page's entries, the route's count)`` from ``GET /v1/catalog/docs/by-meta``, as many requests as *limit* needs.

    *cat* must have ``documents_by_meta``. The engine applied ``offset`` and ``limit``, so the rows are not sliced
    again, but every clause sent is checked again on the client (the meta pairs, ``sensitive`` is ``True``, the
    ``content_type``), so an engine fault cannot widen the answer. A ``--limit`` below 1, an ``--offset`` below 0 and
    an empty ``--meta`` value are usage errors, raised before any request. An answer the entries cannot be built
    from (a tumbler that does not parse) is a :class:`click.ClickException` naming the route. Raises
    :class:`EngineRouteUnavailable` when the engine gives no answer; the caller words it.
    """
    from nexus.catalog.meta_filter import documents_with_meta  # noqa: PLC0415 — deferred to avoid CLI startup cost

    if limit < 1 or offset < 0:
        raise click.UsageError("--limit must be at least 1 and --offset at least 0 with --meta")
    for key, value in meta_wanted:
        if not key or value == "":
            raise click.UsageError(f"--meta: a metadata clause needs a key and a non-empty value, got {key!r}={value!r}")
    entries: list[CatalogEntry] = []
    count = 0
    while len(entries) < limit:
        try:
            page, count = cat.documents_by_meta(
                content_type=content_type or None, meta=meta_wanted, sensitive=sensitive,
                limit=min(DOC_PAGE_MAX, limit - len(entries)), offset=offset + len(entries))
        except ValueError as exc:  # the arguments were checked above, so this is the answer's entries
            raise click.ClickException(
                f"the engine's answer to GET /v1/catalog/docs/by-meta is not the route's contract ({exc})") from exc
        entries += page
        if not page or offset + len(entries) >= count:
            break
    kept = documents_with_meta(entries, meta_wanted)
    if sensitive:
        kept = [e for e in kept if e.sensitive is True]
    if content_type:
        kept = [e for e in kept if e.content_type == content_type]
    return kept, count


def meta_page(
    cat: "CatalogReader", *, content_type: str, meta_wanted: list[tuple[str, str]], sensitive: bool, offset: int, limit: int,
) -> "tuple[list[CatalogEntry], int]":
    """``(the page's entries, the route's count)`` for ``list --meta`` with no ``--owner``: :func:`meta_rows`, with an
    engine that gives no answer reported as an error naming the route and the cause."""
    if not callable(getattr(cat, "documents_by_meta", None)):
        raise click.ClickException(NEEDS_ROUTE)
    try:
        return meta_rows(cat, content_type=content_type, meta_wanted=meta_wanted, sensitive=sensitive, offset=offset, limit=limit)
    except EngineRouteUnavailable as exc:
        raise click.ClickException(f"nx catalog list --meta: {exc}") from exc
