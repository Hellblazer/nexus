# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tombstoned catalog documents, as the maintenance verbs need to see them.

A deleted catalog document is a tombstone (``deleted_at`` set) until
``purge_trash`` reclaims it, and its chunks stay stored for the same window.
Since RDR-192 the maintenance verbs read STORED chunks (``include_non_live``),
so they meet those chunks again. Their "already registered?" lookups exclude
tombstones by design, so a verb that took the absence of a live row for a gap
would register, or re-index, a document somebody deliberately deleted
(nexus-wbfpw.35 fix round 2).

This module is the one include-deleted lookup those verbs share: it reads the
engine's trash listing (``GET /v1/catalog/trash``) once and answers "is this
path a tombstoned document of this owner / this collection?".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)

_PAGE = 300


@dataclass(frozen=True)
class TombstonedDocument:
    tumbler: str
    owner: str  # the document tumbler minus its last segment
    file_path: str
    physical_collection: str
    content_type: str


@dataclass
class Tombstones:
    docs: list[TombstonedDocument] = field(default_factory=list)

    def covers_path(
        self, paths: tuple[str, ...], *, owner: str = "", collection: str = "",
    ) -> bool:
        """True when a tombstoned document at one of *paths* sits under *owner*
        (when given) and in *collection* (when given).

        A catalog ``file_path`` is repo-relative while a chunk's ``source_path``
        is usually absolute, so a path also matches when it ends in
        ``/<file_path>``.
        """
        for d in self.docs:
            if not d.file_path:
                continue
            if owner and d.owner != owner:
                continue
            if collection and d.physical_collection != collection:
                continue
            for p in paths:
                if p == d.file_path or p.endswith("/" + d.file_path):
                    return True
        return False

    def covers_collection(self, *, owner: str, collection: str, content_type: str = "") -> bool:
        """True when *owner* has a tombstoned document in *collection* (of
        *content_type*, when given). For the verbs that register one document
        per collection rather than per path."""
        return any(
            d.owner == owner and d.physical_collection == collection
            and (not content_type or d.content_type == content_type)
            for d in self.docs
        )


def read_tombstones(cat: Any) -> Tombstones:
    """Every tombstoned document the catalog still holds, paged.

    An engine that predates the trash route (404) answers with an empty set and
    a warning: the verb then behaves as it did before this guard, rather than
    refusing to run. An entry without ``file_path`` (an engine predating that
    field) cannot be matched by path; it is counted in the warning.
    """
    lister = getattr(cat, "list_trash", None)
    if lister is None:
        return Tombstones()
    docs: list[TombstonedDocument] = []
    offset = 0
    pathless = 0
    while True:
        try:
            page = lister(limit=_PAGE, offset=offset)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                _log.warning(
                    "tombstone_listing_unavailable",
                    detail="engine has no /v1/catalog/trash route; the verb cannot "
                           "tell a deleted document from a gap",
                )
                return Tombstones()
            raise
        for d in page:
            tumbler = str(d.get("tumbler") or "")
            fp = d.get("file_path")
            if fp is None:
                pathless += 1
            docs.append(TombstonedDocument(
                tumbler=tumbler,
                owner=tumbler.rsplit(".", 1)[0] if "." in tumbler else "",
                file_path=str(fp or ""),
                physical_collection=str(d.get("physical_collection") or ""),
                content_type=str(d.get("content_type") or ""),
            ))
        if len(page) < _PAGE:
            break
        offset += _PAGE
    if pathless:
        _log.warning(
            "tombstone_listing_without_file_path",
            entries=pathless,
            detail="engine predates the file_path field on /v1/catalog/trash; "
                   "those tombstones cannot be matched by path",
        )
    return Tombstones(docs)
