# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The rows of ``nx catalog list --sensitive`` without ``--owner`` or ``--meta`` (RDR-241 Phase 1, nexus-uwggq.1.7).

The engine's bounded sensitive list (``GET /v1/catalog/sensitive``, engine 0.1.188), then ``resolve_many`` for the
tumblers a page or a filter needs, in place of reading every document of the type. The list comes back in the
engine's tumbler order (``ORDER BY tumbler``, the order the walk's ``/list`` has) and its ``count`` is exact even
when the list is cut at the limit, and ``resolve_many`` answers in no order, so the rows are put back in the
list's order.

The list is capped at 10,000 and cannot filter by ``--type``, so a page that the list cannot serve goes to the
metadata route's own ``sensitive`` clause (``meta_listing.meta_rows`` with no meta pairs), which has no cap and takes
``content_type`` (RDR-241 Phase 2, Sam's ask uwggq17-sensitive-cap option C): ``--type`` asks the metadata route
first, and a page past the cap or a capped list that needs the whole list asks it instead of refusing. An engine
without the metadata route keeps the Phase 1 answer and its refusal, the refusal saying the route is missing too.
There is no walk: an engine that cannot answer is an error saying so.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import click

from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable
from nexus.catalog.sensitivity_doctor import SensitiveListUnavailable
from nexus.commands.catalog_cmds.meta_listing import meta_rows

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader
    from nexus.catalog.types import CatalogEntry

#: The most documents the engine lists in one answer (``CatalogSensitiveReads.MAX_LIMIT``); the same bound the
#: doctor rows ask for.
SENSITIVE_LIST_LIMIT: int = 10_000

BY_META_ROUTE = "GET /v1/catalog/docs/by-meta"

NEEDS_ROUTE = "nx catalog list --sensitive needs an engine with the sensitive list route (engine-service 0.1.188 or later)"
PROBE_FAILED = "nx catalog list --sensitive: the engine's capabilities could not be read; try again"
TIMED_OUT = "nx catalog list --sensitive: the engine could not answer in time; try again, or narrow with --owner"
FAILED = "nx catalog list --sensitive: the engine could not be reached or failed; try again"
UNREADABLE = "nx catalog list --sensitive: the engine's sensitive list answer could not be read; try again"
_MESSAGES = {
    SensitiveListUnavailable.UNSUPPORTED: NEEDS_ROUTE,
    SensitiveListUnavailable.PROBE_FAILED: PROBE_FAILED,
    SensitiveListUnavailable.TIMEOUT: TIMED_OUT,
    SensitiveListUnavailable.FAILED: FAILED,
    SensitiveListUnavailable.MALFORMED: UNREADABLE,
}


class _NeedsMoreThanTheList(Exception):
    """The sensitive list is capped and the page needs more of it than the first 10000; carries the refusal's reason."""


def sensitive_page(
    cat: "CatalogReader", *, content_type: str, offset: int, limit: int,
) -> "tuple[list[CatalogEntry], int]":
    """``(the page's entries, the total they page)`` for ``list --sensitive`` with no ``--owner`` and no ``--meta``.

    ``--type`` on a page the metadata route can serve (``--limit`` at least 1, ``--offset`` at least 0) asks that
    route first (:func:`_by_meta`). Otherwise the sensitive list is asked for the first ``offset + limit`` tumblers
    (at most 10000), only ``tumblers[offset:offset + limit]`` are resolved, and the total is the list's count, which
    is exact when the list is capped: a first page works on a tenant of any size. A page the list cannot serve (it
    is capped and the page reaches past its first 10000, or ``--type`` needs the whole list of a capped one) is asked
    of the metadata route; an engine without it gets the refusal, which says the route is missing. A negative
    ``--offset`` or ``-n`` is read as the whole list, so the slice means what it meant on the walk, and the metadata
    route has no such page. A listed tumbler that ``resolve_many`` no longer returns, or returns no longer flagged
    (deleted or cleared between the two reads), is left out.
    """
    pageable = limit >= 1 and offset >= 0
    asked_by_meta = False
    if content_type and pageable:
        if (got := _by_meta(cat, content_type=content_type, offset=offset, limit=limit)) is not None:
            return got
        asked_by_meta = True
    try:
        return _from_sensitive_list(cat, content_type=content_type, offset=offset, limit=limit)
    except _NeedsMoreThanTheList as exc:
        if pageable and not asked_by_meta and (got := _by_meta(cat, content_type=content_type, offset=offset, limit=limit)) is not None:
            return got
        missing = f"; the engine has no {BY_META_ROUTE} either" if pageable else ""
        raise click.ClickException(
            f"nx catalog list --sensitive: this tenant has more than {SENSITIVE_LIST_LIMIT} sensitive documents "
            f"and {exc}{missing}; narrow with --owner"
        ) from None


def _by_meta(cat: "CatalogReader", *, content_type: str, offset: int, limit: int) -> "tuple[list[CatalogEntry], int] | None":
    """The page from the metadata route's ``sensitive`` clause, or ``None`` when the engine does not have the route
    (a reader without the method, an engine without its flag, or a 404): the Phase 1 answer stands then. Any other
    failure of the route is an error naming it."""
    if not callable(getattr(cat, "documents_by_meta", None)):
        return None
    try:
        return meta_rows(cat, content_type=content_type, meta_wanted=[], sensitive=True, offset=offset, limit=limit)
    except EngineRouteUnavailable as exc:
        if exc.reason == EngineRouteUnavailable.UNSUPPORTED:
            return None
        raise click.ClickException(f"nx catalog list --sensitive: {exc}") from exc


def _from_sensitive_list(
    cat: "CatalogReader", *, content_type: str, offset: int, limit: int,
) -> "tuple[list[CatalogEntry], int]":
    """The Phase 1 path: the sensitive list, then ``resolve_many``. Raises :class:`_NeedsMoreThanTheList` for a page
    the capped list cannot serve."""
    listing = getattr(cat, "sensitive_documents", None)
    if not callable(listing):
        raise click.ClickException(NEEDS_ROUTE)
    whole = bool(content_type) or offset < 0 or limit < 0
    asked = SENSITIVE_LIST_LIMIT if whole else max(1, min(offset + limit, SENSITIVE_LIST_LIMIT))
    try:
        answer = listing(asked, strict=True)
    except SensitiveListUnavailable as exc:
        raise click.ClickException(_MESSAGES[exc.reason]) from exc
    if answer["capped"] and (whole or offset + limit > SENSITIVE_LIST_LIMIT):
        raise _NeedsMoreThanTheList(
            "--type needs the whole list" if content_type
            else "a negative --offset or -n needs the whole list" if whole
            else f"this page reaches past the first {SENSITIVE_LIST_LIMIT}")
    tumblers = [d["tumbler"] for d in answer["documents"]]
    wanted = tumblers if whole else tumblers[offset:offset + limit]
    resolved = cat.resolve_many(wanted) if wanted else {}
    # A row cleared between the two reads is not sensitive any more: the walk kept only flagged rows, so does this.
    entries = [resolved[t] for t in wanted if t in resolved and resolved[t].sensitive]
    if not whole:
        return entries, answer["count"]
    if content_type:
        entries = [e for e in entries if e.content_type == content_type]
    return entries[offset:offset + limit], len(entries)
