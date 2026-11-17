# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The rows of ``nx catalog list --sensitive`` without ``--owner`` (RDR-241 Phase 1, nexus-uwggq.1.7).

The engine's bounded sensitive list (``GET /v1/catalog/sensitive``, engine 0.1.188), then ``resolve_many`` for the
tumblers a page or a filter needs, in place of reading every document of the type. The list comes back in the
engine's tumbler order (``ORDER BY tumbler``, the order the walk's ``/list`` has), and ``resolve_many`` answers in
no order, so the rows are put back in the list's order. There is no fallback: an engine that cannot answer is an
error saying so, never a walk.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import click

from nexus.catalog.sensitive_list import SensitiveListUnavailable

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader
    from nexus.catalog.types import CatalogEntry

#: The most documents the engine lists in one answer (``CatalogSensitiveReads.MAX_LIMIT``); the same bound the
#: doctor rows ask for. A tenant with more is told to narrow with ``--owner``.
SENSITIVE_LIST_LIMIT: int = 10_000

NEEDS_ROUTE = "nx catalog list --sensitive needs an engine with the sensitive list route (engine-service 0.1.188 or later)"
_TIMED_OUT = "nx catalog list --sensitive: the engine could not answer in time; try again, or narrow with --owner"
_UNREADABLE = "nx catalog list --sensitive: the engine's sensitive list answer could not be read; try again, or narrow with --owner"


def sensitive_page(
    cat: "CatalogReader", *, content_type: str, meta_wanted: list[tuple[str, str]], offset: int, limit: int,
) -> "tuple[list[CatalogEntry], int]":
    """``(the page's entries, the total they page)`` for ``list --sensitive`` with no ``--owner``.

    Without ``--type`` or ``--meta`` only ``tumblers[offset:offset + limit]`` are resolved and the total is the
    route's count. With either, every listed tumbler is resolved, the type and meta filters apply, and the page is
    cut from the matches. A listed tumbler that ``resolve_many`` no longer returns (deleted between the two reads)
    is left out.
    """
    from nexus.catalog.meta_filter import documents_with_meta  # noqa: PLC0415 — deferred to avoid CLI startup cost

    listing = getattr(cat, "sensitive_documents", None)
    if not callable(listing):
        raise click.ClickException(NEEDS_ROUTE)
    try:
        answer = listing(SENSITIVE_LIST_LIMIT, strict=True)
    except SensitiveListUnavailable as exc:
        message = {SensitiveListUnavailable.UNSUPPORTED: NEEDS_ROUTE, SensitiveListUnavailable.TIMEOUT: _TIMED_OUT}
        raise click.ClickException(message.get(exc.reason, _UNREADABLE)) from exc
    if answer["capped"]:
        raise click.ClickException(
            f"nx catalog list --sensitive: this tenant has more than {SENSITIVE_LIST_LIMIT} sensitive documents; "
            "narrow with --owner"
        )
    tumblers = [d["tumbler"] for d in answer["documents"]]
    filtered = bool(content_type or meta_wanted)
    wanted = tumblers if filtered else tumblers[offset:offset + limit]
    resolved = cat.resolve_many(wanted) if wanted else {}
    # A row cleared between the two reads is not sensitive any more: the walk kept only flagged rows, so does this.
    entries = [resolved[t] for t in wanted if t in resolved and resolved[t].sensitive]
    if not filtered:
        return entries, answer["count"]
    if content_type:
        entries = [e for e in entries if e.content_type == content_type]
    if meta_wanted:
        entries = documents_with_meta(entries, meta_wanted)
    return entries[offset:offset + limit], len(entries)
