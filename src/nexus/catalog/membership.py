# SPDX-License-Identifier: AGPL-3.0-or-later
"""Known-vs-unknown collection helper (nexus-3ygp3, nexus-v1zdu).

Several ``nx catalog`` / ``nx enrich`` / ``nx t3`` verbs call
``list_by_collection`` (or a manifest-chash read keyed the same way) and
treat a zero-length result as "nothing to do here", reporting success at
exit 0. That is correct when the collection IS registered in the catalog's
collections projection and is genuinely empty — freshly created, fully
migrated away, tombstoned. It is WRONG when the name itself is unknown to
the catalog: a bare subject name where the catalog keys on the physical
four-segment name (``knowledge`` vs
``knowledge__x__voyage-context-3__v1``), a typo, or a collection that was
never indexed reads as a completed, zero-cost operation instead of the
refusal it should be.

nexus-3ygp3 fixed two ``nx enrich aspects`` sites this way but the shared
helper it asked for was never written, so ``nx catalog audit-membership``
and ``nx catalog migrate-fallback`` kept the false-clean shape, and
``nx t3 gc``'s manifest-less-note / RUNFENCE protections silently pass
for an unknown collection instead of refusing the run (nexus-v1zdu).

This module answers ONLY the known-vs-unknown question and renders the
shared message; whether an empty-but-KNOWN collection is itself worth
refusing is a per-verb decision the caller makes (``nx enrich aspects``
refuses either way — there is nothing to extract regardless of cause;
``nx catalog audit-membership`` reports a known-empty collection
informationally and refuses only the unknown case).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader

_log = structlog.get_logger(__name__)


def collection_is_known(catalog: "CatalogReader", collection: str) -> bool:
    """True if *collection* is registered in the catalog's collections
    projection (``get_collection`` returns a row), regardless of whether
    it currently holds any documents.
    """
    return catalog.get_collection(collection) is not None


def unknown_collection_message(
    collection: str, *, orphan_rows: int | None = None, row_label: str = "row",
) -> str:
    """The refusal text for a *collection* name the catalog does not know.

    Shared across every ``list_by_collection``-style caller that must
    distinguish "known, zero entries" from "unknown" (nexus-3ygp3,
    nexus-v1zdu). *orphan_rows*, when given, names side-table rows (e.g.
    ``document_aspects``) that exist under this exact name with no
    catalog entry to claim them, surfaced the way the aspects
    ``--missing`` audit does. *row_label* names what kind of row those
    are (e.g. ``"aspect row"``) for callers with side-table context more
    specific than the generic default.
    """
    msg = (
        f"No catalog rows in {collection!r}. Pass the physical collection "
        "name as `nx collection list` prints it; a collection that is "
        "genuinely empty has nothing to audit, migrate, or extract."
    )
    if orphan_rows:
        msg += (
            f" {orphan_rows} {row_label}(s) exist under that exact name and "
            "match no catalog entry."
        )
    return msg


def refuse_if_collection_unknown(
    catalog: "CatalogReader",
    collection: str,
    entries: object,
    *,
    known: bool | None = None,
    orphan_rows: int | None = None,
) -> None:
    """Raise ``click.ClickException`` when *entries* is empty AND
    *collection* is not registered in the catalog.

    A registered collection with zero entries is left alone — the caller
    decides whether "genuinely empty" is itself worth refusing (as
    ``nx enrich aspects`` does, unconditionally, since there is nothing
    productive to do either way) or worth reporting informationally (as
    ``nx catalog audit-membership`` does). This helper only closes the
    false-clean gap where an UNKNOWN name reads as empty-and-fine.

    *known*, when given, skips the ``get_collection`` round trip — pass
    the already-fetched result's non-``None``-ness when the caller made
    that call itself (``nx catalog migrate-fallback`` already refuses an
    unregistered source before ever calling ``list_by_collection``).
    """
    import click  # noqa: PLC0415 — command-layer dependency kept out of the module's import-time surface

    if entries:
        return
    is_known = known if known is not None else collection_is_known(catalog, collection)
    if is_known:
        return
    _log.warning("catalog_collection_unknown", collection=collection)
    raise click.ClickException(unknown_collection_message(collection, orphan_rows=orphan_rows))
