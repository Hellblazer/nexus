# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve a tumbler, owner or corpus to physical T3 collection names (GH #1668).

One function behind the MCP tool ``resolve`` and the CLI ``nx catalog resolve``,
so the two cannot drift.

``corpus`` has two meanings:

* a free-form document ``corpus`` label (``cat.by_corpus``), matched exactly;
* a collection content type (``code``, ``docs``, ``rdr``, ``knowledge``, the
  ``nexus.corpus.CONTENT_TYPES`` grammar): the live collections of that type
  (``cat.list_collections(lifecycle_state="live", content_type=...)``).

The indexers do not set a document's label to ``rdr`` by default, so before GH #1668
``corpus="rdr"`` matched nothing and the empty answer looked like "no such
collections". A value that is neither a content type nor a label any document
carries now raises :class:`UnknownCorpusError`; a content type with no live
collections is a true empty list.

No client walk of the catalog (RDR-241): only ``list_collections`` and
``by_corpus``, which the service answers by one filtered query each.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.corpus import CONTENT_TYPES

if TYPE_CHECKING:
    from nexus.catalog.tumbler import Tumbler


class UnknownCorpusError(ValueError):
    """``corpus`` is not a content type and no document carries it as a label."""


def parse_user_tumbler(raw: str, field: str) -> "Tumbler":
    """Parse a caller-supplied tumbler, or raise a ValueError that says why.

    ``Tumbler.parse`` leaks int()'s "invalid literal for int() with base 10"
    for anything that is not dotted integers (nexus-zdzm5). The dashed form
    ``nx doctor`` prints ("1-2188") is a physical collection prefix, not a
    tumbler.
    """
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred: heavy catalog import, branch-local

    try:
        return Tumbler.parse(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{field}={raw!r}: not a dotted tumbler (e.g. '1.2.3'). "
            f"If you have a physical collection prefix like "
            f"'1-2188' from `nx doctor`, that is NOT a tumbler. "
            f"underlying: {exc}"
        ) from exc


def unknown_corpus_message(corpus: str) -> str:
    """The text for an unknown ``corpus`` value: the value, the valid content types, and the label rule."""
    return (
        f"corpus={corpus!r}: not a collection content type and no document carries it as a "
        f"corpus label. Valid content types: {', '.join(CONTENT_TYPES)}. "
        "Free-form corpus labels are matched exactly."
    )


def collections_for_corpus(cat: Any, corpus: str) -> set[str]:
    """Collection names for *corpus*: label matches, plus live collections of that content type.

    Raises :class:`UnknownCorpusError` when *corpus* is not a content type and
    matches no document's label.
    """
    by_label = cat.by_corpus(corpus)
    names = {e.physical_collection for e in by_label if e.physical_collection}
    if corpus in CONTENT_TYPES:
        for row in cat.list_collections(lifecycle_state="live", content_type=corpus):
            if row.get("name"):
                names.add(row["name"])
    elif not by_label:
        raise UnknownCorpusError(unknown_corpus_message(corpus))
    return names


def resolve_collections(
    cat: Any, *, tumbler: str = "", owner: str = "", corpus: str = "",
) -> list[str]:
    """Sorted distinct physical collection names for a document, owner and/or corpus.

    The union of whatever was given. Entries with no ``physical_collection`` are
    skipped. Raises ``ValueError`` for an unparsable tumbler or an unknown owner
    (message begins with the argument name) and :class:`UnknownCorpusError` for
    an unknown ``corpus``.
    """
    collections: set[str] = set()
    if tumbler:
        from nexus.catalog import entry_for_source_uri, looks_like_source_uri  # noqa: PLC0415 — deferred: heavy catalog import, branch-local

        if looks_like_source_uri(tumbler):
            entry = entry_for_source_uri(cat, tumbler)
        else:
            parsed = parse_user_tumbler(tumbler, "tumbler")
            entry = cat.resolve(parsed)
        if entry and entry.physical_collection:
            collections.add(entry.physical_collection)
    if owner:
        # GH #1527 (nexus-qiah5): a registered owner NAME is accepted too;
        # the owner table maps it to the tumbler.
        from nexus.catalog.owner_scope import OwnerScopeError, resolve_owner_scope  # noqa: PLC0415 — deferred, branch-local

        try:
            owner_tumbler = resolve_owner_scope(cat, owner)
        except OwnerScopeError as exc:
            raise ValueError(f"owner {exc}") from exc
        for e in cat.by_owner(parse_user_tumbler(owner_tumbler, "owner")):
            if e.physical_collection:
                collections.add(e.physical_collection)
    if corpus:
        collections |= collections_for_corpus(cat, corpus)
    return sorted(collections)
