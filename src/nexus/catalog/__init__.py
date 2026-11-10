# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from nexus.catalog.types import CatalogEntry, CatalogLink
from nexus.catalog.tumbler import (
    DocumentRecord,
    LinkRecord,
    OwnerRecord,
    Tumbler,
)

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader

__all__ = [
    "CatalogEntry",
    "CatalogLink",
    "DocumentRecord",
    "LinkRecord",
    "OwnerRecord",
    "Tumbler",
    "entry_for_source_uri",
    "looks_like_source_uri",
    "resolve_tumbler",
]


_DT_SCHEME: str = "x-devonthink-item://"
_DT_UUID_RE: re.Pattern[str] = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)


def _devonthink_canonical(value: str) -> str | None:
    """The stored form of a DEVONthink handle: ``x-devonthink-item://<UPPER-UUID>``.

    DEVONthink writes the UUID upper case; a caller may hold it lower case, bare,
    or as an item link with ``?page=`` / ``#`` suffixes. ``None`` when *value* is
    neither a bare UUID nor an ``x-devonthink-item://`` link.
    """
    if value.lower().startswith(_DT_SCHEME):
        body = re.split(r"[?#/]", value[len(_DT_SCHEME):], maxsplit=1)[0]
    else:
        body = value
    if not _DT_UUID_RE.fullmatch(body):
        return None
    return _DT_SCHEME + body.upper()


def entry_for_source_uri(cat: "CatalogReader", value: str) -> CatalogEntry | None:
    """The document whose ``source_uri`` is *value*, when *value* is a URI or a DEVONthink UUID.

    A DEVONthink record is keyed by ``x-devonthink-item://<uuid>``; its
    ``file_path`` is the database package path, so the URI is the handle a
    caller holds. Every scheme matches exactly; a DEVONthink handle is also
    tried in its stored form (see :func:`_devonthink_canonical`). Anything else
    (a title, a path) is not tried. Only ``source_uri`` is read: a record whose
    DEVONthink stamp failed keeps a ``file://`` URI and is found by its path.
    """
    value = value.strip()
    dt = _devonthink_canonical(value)
    if "://" not in value and dt is None:
        return None
    for candidate in dict.fromkeys(c for c in (value, dt) if c and "://" in c):
        if (entry := cat.by_source_uri(candidate)) is not None:
            return cast("CatalogEntry", entry)
    return None


def looks_like_source_uri(value: str) -> bool:
    """True when *value* is shaped like a source URI or a DEVONthink UUID."""
    value = value.strip()
    return "://" in value or _devonthink_canonical(value) is not None


def resolve_tumbler(
    cat: "CatalogReader", value: str
) -> tuple[Tumbler | None, str | None]:
    """Resolve a tumbler string, source URI, or title/filename to a ``(Tumbler, None)`` pair.

    Returns ``(None, error_message)`` on failure.
    """
    try:
        t = Tumbler.parse(value)
        if cat.resolve(t) is not None:
            return t, None
        return None, f"Not found: {value!r}"
    except ValueError:
        pass
    if (by_uri := entry_for_source_uri(cat, value)) is not None:
        return by_uri.tumbler, None
    if looks_like_source_uri(value):
        # A URI that names no document is absent; a title search over its
        # tokens would only find some other document.
        return None, f"Not found: {value!r}"
    # Every match (nexus-3bafq): the exact-title pick and the ambiguity
    # count are both wrong over the engine's 50-row default page.
    results = cat.find_all(value)
    if results:
        exact = [r for r in results if r.title == value]
        if exact:
            return exact[0].tumbler, None
        if len(results) == 1:
            return results[0].tumbler, None
        return None, f"Ambiguous: {len(results)} documents match {value!r} — use tumbler"
    return None, f"Not found: {value!r}"
