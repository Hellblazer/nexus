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

import click
import httpx
import structlog

_log = structlog.get_logger(__name__)

_PAGE = 300

#: What a refusal tells the operator. ``/v1/catalog/trash`` entries carry
#: ``file_path`` only from the engine release after ``engine-service-v0.1.142``.
_ENGINE_NEEDED = (
    "an engine newer than engine-service-v0.1.142 (the first release whose "
    "/v1/catalog/trash entries carry file_path)"
)


class TombstoneGuardUnavailable(click.ClickException):
    """The engine cannot tell the caller which paths are deleted documents.

    Raised, never logged-and-continued: the verbs that read the trash are
    destructive or re-registering (``nx collection reindex`` purges a collection
    and rebuilds from the paths it kept; the backfills register a document for
    every stored path with no live row), and without the listing they cannot tell
    a deleted document from a gap. Failing closed costs a re-run after the engine
    upgrade; failing open revives a document somebody deleted on purpose.
    """


@dataclass(frozen=True)
class TombstonedDocument:
    tumbler: str
    owner: str  # the document tumbler minus its last segment
    file_path: str
    physical_collection: str
    content_type: str
    title: str = ""


@dataclass
class Tombstones:
    docs: list[TombstonedDocument] = field(default_factory=list)

    def covers_path(
        self, paths: tuple[str, ...], *, owner: str = "", collection: str = "",
    ) -> bool:
        """True when a tombstoned document whose ``file_path`` EQUALS one of
        *paths* sits under *owner* (when given) and in *collection* (when given).

        Exact comparison, never a suffix: a catalog ``file_path`` is
        repo-relative, and ``README.md`` is a suffix of every
        ``pkg/.../README.md``. The caller passes every form of the path it holds
        (absolute and owner-relative) and the match is on equality with one of
        them.
        """
        wanted = {p for p in paths if p}
        for d in self.docs:
            if not d.file_path or d.file_path not in wanted:
                continue
            if owner and d.owner != owner:
                continue
            if collection and d.physical_collection != collection:
                continue
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

    Raises :class:`TombstoneGuardUnavailable` when the engine cannot answer with
    paths: the reader has no ``list_trash``, the engine has no trash route (404),
    or an entry lacks the ``file_path`` KEY (an engine predating the field). A
    ``null`` ``file_path`` is a different thing and is accepted: a tombstoned
    paper or note never had one.

    The listing is paged by offset over ``ORDER BY deleted_at DESC, tumbler``, so a
    document restored or purged between two pages can make the next page skip a
    row. The effect is one tombstone fewer in the guard (a deleted document may be
    revived; nothing is dropped), the same as a delete that lands after this read,
    which no paging could see. A keyset cursor would need an engine parameter and a
    loop guard against an engine that ignores it; it is not done.
    """
    lister = getattr(cat, "list_trash", None)
    if lister is None:
        raise TombstoneGuardUnavailable(
            "this catalog reader cannot list deleted documents, so the verb cannot "
            "tell a deleted document from a gap; refusing."
        )
    docs: list[TombstonedDocument] = []
    offset = 0
    while True:
        try:
            page = lister(limit=_PAGE, offset=offset)
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise TombstoneGuardUnavailable(
                    "the engine has no /v1/catalog/trash route, so the verb cannot "
                    "tell a deleted document from a gap; refusing. Needs "
                    f"{_ENGINE_NEEDED}."
                ) from exc
            raise
        for d in page:
            if "file_path" not in d:
                raise TombstoneGuardUnavailable(
                    "the engine's /v1/catalog/trash entries carry no file_path, so "
                    "the verb cannot tell a deleted document from a gap; refusing. "
                    f"Needs {_ENGINE_NEEDED}."
                )
            tumbler = str(d.get("tumbler") or "")
            docs.append(TombstonedDocument(
                tumbler=tumbler,
                owner=tumbler.rsplit(".", 1)[0] if "." in tumbler else "",
                file_path=str(d.get("file_path") or ""),
                physical_collection=str(d.get("physical_collection") or ""),
                content_type=str(d.get("content_type") or ""),
                title=str(d.get("title") or ""),
            ))
        if len(page) < _PAGE:
            break
        offset += _PAGE
    return Tombstones(docs)


def has_live_document(
    cat: Any, paths: tuple[str, ...], *, owner: str = "", collection: str = "",
) -> bool:
    """True when the catalog holds a LIVE document at one of *paths*.

    Two scopes, because the two kinds of caller need opposite things:

    * ``owner`` given: the document must be live at exactly ``(owner, path)``,
      the catalog's own identity for a file. The backfills use this. They skip a
      path whose tombstone is under the owner they would register it under, unless
      a live document already holds it there (delete, then index the file again:
      the tombstone and the live row sit side by side). Skipping is the safe
      direction for them, so another owner's live document is no reason to
      register a second one.
    * ``owner`` empty: ANY live document, whatever its owner, narrowed to
      *collection* when given. ``nx collection reindex`` uses this. One file can
      live under two owners (nexus-z0lu4, ``find_all_by_file_path``), so a
      tombstone at one owner says nothing about a live document at another, and
      dropping the path purges chunks that live document owns. Keeping is the safe
      direction there.

    Live rows only in both scopes: the catalog's lookups exclude tombstones.
    """
    wanted = [p for p in dict.fromkeys(paths) if p]
    if owner:
        return any(cat.by_file_path(owner, p) is not None for p in wanted)
    for p in wanted:
        for e in cat.find_all_by_file_path(p):
            if not collection or e.physical_collection == collection:
                return True
    return False


def deleted_only_sources(
    cat: Any, tombstones: Tombstones, sources: set[str] | list[str], *, collection: str,
) -> list[str]:
    """The *sources* (chunk ``source_path`` values, usually absolute) whose only
    catalog presence in *collection* is a tombstone.

    A source matches one tombstone when it EQUALS its ``file_path`` or, once made
    relative to THAT tombstone owner's ``repo_root``, equals it: the catalog's
    normalised repo-relative form, compared exactly, and never through another
    owner's root. A matched source is kept (not returned) when any live catalog
    document in *collection* names it, under any owner and in either form: the
    tombstone's owner is not the only owner a file can have.
    """
    from nexus.repo_identity import owner_repo_root_best_effort  # noqa: PLC0415 — deferred: keeps catalog import light

    here = [d for d in tombstones.docs if d.physical_collection == collection and d.file_path]
    roots: dict[str, str] = {}
    out: list[str] = []
    for sp in sorted(sources):
        forms = {sp}
        matched = False
        for d in here:
            if sp == d.file_path:
                matched = True
                continue
            if d.owner not in roots:
                roots[d.owner] = owner_repo_root_best_effort(cat, d.owner).rstrip("/")
            root = roots[d.owner]
            if root and sp.startswith(root + "/") and sp[len(root) + 1:] == d.file_path:
                matched = True
                forms.add(d.file_path)
        if matched and not has_live_document(cat, tuple(forms), collection=collection):
            out.append(sp)
    return out
