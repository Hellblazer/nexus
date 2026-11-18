# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The rows of ``nx catalog list --sensitive`` without ``--owner`` (RDR-241 Phase 1, nexus-uwggq.1.7).

The engine's bounded sensitive list (``GET /v1/catalog/sensitive``, engine 0.1.188), then ``resolve_many`` for the
tumblers a page or a filter needs, in place of reading every document of the type. The list comes back in the
engine's tumbler order (``ORDER BY tumbler``, the order the walk's ``/list`` has) and its ``count`` is exact even
when the list is cut at the limit, and ``resolve_many`` answers in no order, so the rows are put back in the
list's order. There is no fallback: an engine that cannot answer is an error saying so, never a walk.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import click

from nexus.catalog.sensitivity_doctor import SensitiveListUnavailable

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader
    from nexus.catalog.types import CatalogEntry

#: The most documents the engine lists in one answer (``CatalogSensitiveReads.MAX_LIMIT``); the same bound the
#: doctor rows ask for.
SENSITIVE_LIST_LIMIT: int = 10_000

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


def sensitive_page(
    cat: "CatalogReader", *, content_type: str, meta_wanted: list[tuple[str, str]], offset: int, limit: int,
) -> "tuple[list[CatalogEntry], int]":
    """``(the page's entries, the total they page)`` for ``list --sensitive`` with no ``--owner``.

    Without ``--type`` or ``--meta`` the route is asked for the first ``offset + limit`` tumblers (at most 10000),
    only ``tumblers[offset:offset + limit]`` are resolved, and the total is the route's count, which is exact when
    the list is capped: a first page works on a tenant of any size. Such a page is refused only when the list is
    capped and the page reaches past its first 10000. With either filter the whole list is needed, so a capped
    answer is refused. A negative ``--offset`` or ``-n`` is read as the whole list too, so the slice means what it
    meant on the walk. A listed tumbler that ``resolve_many`` no longer returns, or returns no longer flagged
    (deleted or cleared between the two reads), is left out.
    """
    from nexus.catalog.meta_filter import documents_with_meta  # noqa: PLC0415 — deferred to avoid CLI startup cost

    listing = getattr(cat, "sensitive_documents", None)
    if not callable(listing):
        raise click.ClickException(NEEDS_ROUTE)
    filtered = bool(content_type or meta_wanted)
    whole = filtered or offset < 0 or limit < 0
    asked = SENSITIVE_LIST_LIMIT if whole else max(1, min(offset + limit, SENSITIVE_LIST_LIMIT))
    try:
        answer = listing(asked, strict=True)
    except SensitiveListUnavailable as exc:
        raise click.ClickException(_MESSAGES[exc.reason]) from exc
    if answer["capped"] and (whole or offset + limit > SENSITIVE_LIST_LIMIT):
        why = ("--type and --meta need the whole list" if filtered
               else "a negative --offset or -n needs the whole list" if whole
               else f"this page reaches past the first {SENSITIVE_LIST_LIMIT}")
        raise click.ClickException(
            f"nx catalog list --sensitive: this tenant has more than {SENSITIVE_LIST_LIMIT} sensitive documents "
            f"and {why}; narrow with --owner"
        )
    tumblers = [d["tumbler"] for d in answer["documents"]]
    wanted = tumblers if whole else tumblers[offset:offset + limit]
    resolved = cat.resolve_many(wanted) if wanted else {}
    # A row cleared between the two reads is not sensitive any more: the walk kept only flagged rows, so does this.
    entries = [resolved[t] for t in wanted if t in resolved and resolved[t].sensitive]
    if not whole:
        return entries, answer["count"]
    if content_type:
        entries = [e for e in entries if e.content_type == content_type]
    if meta_wanted:
        entries = documents_with_meta(entries, meta_wanted)
    return entries[offset:offset + limit], len(entries)
