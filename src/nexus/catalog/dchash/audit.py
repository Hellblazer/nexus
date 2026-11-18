# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 5: the stale dchash report behind ``link_audit``.

:func:`dchash_audit` checks every ``dchash:`` span of a list of links against its
own side's document. Spans are grouped by document, each document is resolved and
read once, and only its hash and length are kept while its spans are checked, so
the audit holds one document's text at a time. The status of each span comes from
:func:`nexus.catalog.dchash.source.span_status`, the mapping ``dchash_status`` uses.

The report reads source files by path, so it describes the machine it runs on.
``httpx.HTTPError`` and ``RuntimeError`` from the catalog propagate (an engine
outage fails the audit instead of listing every span as stale); only a file that
cannot be read or decoded becomes ``unreadable``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.catalog.dchash.source import (
    MALFORMED,
    OK,
    dchash_source,
    read_doc,
    span_status,
)
from nexus.catalog.dchash.text import DCHASH_PREFIX

if TYPE_CHECKING:
    from nexus.catalog.types import CatalogLink


def has_dchash_span(link: CatalogLink) -> bool:
    """True when either side of *link* carries a ``dchash:`` span."""
    return any(
        isinstance(span, str) and span.startswith(DCHASH_PREFIX)
        for span in (link.from_span, link.to_span)
    )


def dchash_audit(links: list[CatalogLink], catalog: Any) -> list[dict]:
    """One row per ``dchash:`` span of *links* whose status is not ``ok``.

    A row is ``{from_tumbler, to_tumbler, link_type, side, span, status}`` with
    tumblers as strings and *side* ``"from"`` or ``"to"``. *catalog* needs
    ``resolve`` and ``resolve_path``.
    """
    groups: dict[str, list[tuple[Any, CatalogLink, str, str]]] = {}
    for lk in links:
        for side, tumbler, span in (
            ("from", lk.from_tumbler, lk.from_span),
            ("to", lk.to_tumbler, lk.to_span),
        ):
            if isinstance(span, str) and span.startswith(DCHASH_PREFIX):
                groups.setdefault(str(tumbler), []).append((tumbler, lk, side, span))

    rows: list[dict] = []
    for items in groups.values():
        # A malformed span is reported without touching the engine or the file,
        # as dchash_status does; the rest of the document's spans share one read.
        checkable = any(span_status(span, None) != MALFORMED for *_, span in items)
        entry = catalog.resolve(items[0][0]) if checkable else None
        doc = (
            read_doc(dchash_source(entry, catalog))
            if entry is not None and checkable
            else None
        )
        for _tumbler, lk, side, span in items:
            status = span_status(span, doc, entry_found=entry is not None)
            if status == OK:
                continue
            rows.append(
                {
                    "from_tumbler": str(lk.from_tumbler),
                    "to_tumbler": str(lk.to_tumbler),
                    "link_type": lk.link_type,
                    "side": side,
                    "span": span,
                    "status": status,
                }
            )
    return rows
