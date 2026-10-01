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


def has_live_document(cat: Any, tombstones: Tombstones, paths: tuple[str, ...], *, owner: str = "", collection: str = "") -> bool:
    """True when the catalog holds a LIVE document at the path a tombstone covers.

    A deleted document and a re-registered one can share an owner and a path
    (delete, then index the file again): the tombstone and the live row sit side by
    side, and the stored chunks collapse to one row owned by both. The tombstone
    must not stand in for the live document, so a verb skips a path only when
    this is False. The lookup is exact on ``(owner, file_path)``, the catalog's
    own identity for a file, and live rows only.
    """
    wanted = {p for p in paths if p}
    for d in tombstones.docs:
        if not d.file_path or d.file_path not in wanted:
            continue
        if owner and d.owner != owner:
            continue
        if collection and d.physical_collection != collection:
            continue
        if cat.by_file_path(d.owner, d.file_path) is not None:
            return True
    return False


def deleted_only_sources(
    cat: Any, tombstones: Tombstones, sources: set[str] | list[str], *, collection: str,
) -> list[str]:
    """The *sources* (chunk ``source_path`` values, usually absolute) whose only
    catalog presence in *collection* is a tombstone.

    A source matches a tombstone when it EQUALS its ``file_path`` or, once made
    relative to the tombstone owner's ``repo_root``, equals it: the catalog's
    normalised repo-relative form, compared exactly. A source with a live document
    at the same ``(owner, file_path)`` is not returned.
    """
    from nexus.repo_identity import owner_repo_root_best_effort  # noqa: PLC0415 — deferred: keeps catalog import light

    roots: dict[str, str] = {}
    out: list[str] = []
    for sp in sorted(sources):
        forms = {sp}
        for d in tombstones.docs:
            if d.physical_collection != collection or not d.file_path:
                continue
            if d.owner not in roots:
                roots[d.owner] = owner_repo_root_best_effort(cat, d.owner).rstrip("/")
            root = roots[d.owner]
            if root and sp.startswith(root + "/"):
                forms.add(sp[len(root) + 1:])
        paths = tuple(forms)
        if tombstones.covers_path(paths, collection=collection) and not has_live_document(
            cat, tombstones, paths, collection=collection,
        ):
            out.append(sp)
    return out
