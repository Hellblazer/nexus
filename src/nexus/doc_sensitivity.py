# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document-keyed sensitivity reads for the pipelines that bypass T3 (RDR-243 Technical Design item 6).

The aspect pipeline, the bib and CrossRef selection, the validation sample frame, the
taxonomy sample names and the ``aspect_readers`` source reads work from a document, not from a
search hit, so the engine's read filter (phase 2) cannot see them. Each asks the CATALOG row
whether the document is sensitive before it reads or sends anything. The flag is never read from
a T3 chunk row: the chunk store is shared by every document with identical text.

Three rules every gate here shares:

* A document with no live catalog row cannot be flagged and is not refused. The caller decides
  what a missing row means (the worker drops it as gone).
* A lookup that fails is :class:`SensitivityUnknown`, never "not sensitive". A gate that cannot
  tell refuses to send; a drain routes the row to its retry.
* Nothing is cached. The flag is one-way and can be set after a row was queued; a cache would
  keep a stale "clear" answer alive past the flag.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import structlog

from nexus.sensitivity import SensitiveDocumentRefused

_log = structlog.get_logger(__name__)

__all__ = [
    "SensitivityUnknown",
    "flagged",
    "flagged_in_collection",
    "flagged_owners_of_chashes",
    "is_sensitive",
    "refuse_if_sensitive",
    "sensitive_chashes",
    "sensitive_tumbler_for_uri",
    "tumbler_for_path",
]


class SensitivityUnknown(RuntimeError):
    """The catalog could not say whether a document is sensitive (it raised, or was unreachable)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"could not read the sensitivity flag from the catalog: {detail}")


def _catalog_reader() -> Any:
    """The catalog read handle. The seam tests patch."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred: keeps this module's import light

    return make_catalog_reader()


def _entry_flag(entry: Any) -> bool:
    return getattr(entry, "sensitive", False) is True


def flagged(tumblers: Iterable[str], *, cat: Any = None) -> set[str]:
    """The subset of *tumblers* whose catalog row is sensitive.

    One batched read for the tumblers the batch route answers. The batch route does not follow
    ``alias_of``: it returns a merged duplicate's own live row, so a row with ``alias_of`` set is
    resolved again with alias following and the canonical's flag counts. A tumbler the route did not
    return at all (a tombstone) is read once on its own. Blank tumblers are ignored. Raises :class:`SensitivityUnknown` when the catalog cannot be read."""
    wanted = sorted({t for t in tumblers if t})
    if not wanted:
        return set()
    try:
        cat = cat if cat is not None else _catalog_reader()
        if cat is None:
            raise RuntimeError("no catalog reader")
        entries = cat.resolve_many(wanted) or {}
        out: set[str] = set()
        for t, e in entries.items():
            if _entry_flag(e):
                out.add(t)
            elif getattr(e, "alias_of", ""):
                # The batch route returns a merged duplicate's own live row, with its own flag, not the
                # canonical's (the canonical owns the chunks and is what gets flagged). Ask for the
                # canonical, following the alias, before saying "not sensitive".
                resolved = cat.resolve(t, follow_alias=True)
                if resolved is not None and _entry_flag(resolved):
                    out.add(t)
        for t in wanted:
            if t in entries:
                continue
            entry = cat.by_doc_id(t)
            if entry is not None and _entry_flag(entry):
                out.add(t)
        return out
    except SensitivityUnknown:
        raise
    except Exception as exc:  # noqa: BLE001 — every failure of the read is "unknown", whatever its class
        raise SensitivityUnknown(f"{type(exc).__name__}: {exc}"[:300]) from exc


def flagged_in_collection(collection: str, *, cat: Any = None) -> list[Any]:
    """The live catalog entries of *collection* whose row is sensitive (one listing read).

    For a verb that rebuilds a whole collection and must know, before it deletes anything, whether it
    would move a flagged document. Raises :class:`SensitivityUnknown` when the catalog cannot be read."""
    try:
        cat = cat if cat is not None else _catalog_reader()
        if cat is None:
            raise RuntimeError("no catalog reader")
        return [e for e in cat.list_by_collection(collection, limit=1_000_000) if _entry_flag(e)]
    except Exception as exc:  # noqa: BLE001 — see flagged()
        raise SensitivityUnknown(f"{type(exc).__name__}: {exc}"[:300]) from exc


def flagged_owners_of_chashes(
    chashes: Iterable[str], *, cat: Any = None, owners: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """``{chash: [sensitive owner tumbler, ...]}`` for each chunk with a sensitive live owner (RDR-243 Phase 2).

    The read side of a chash-keyed hydration: the engine's ``exclude`` filter drops these chunks, and this is the
    catalog's own answer, so a surface that can name the withheld document does and one whose engine cannot
    filter (a double, a stale engine) still refuses. A chunk shared with a plain document counts, as it does
    for the engine's ``NOT EXISTS``: the shared row cannot be told apart.

    *owners* is a chash -> owner-tumblers map the caller already holds (``store_get_many``'s owner rows); with
    none, ``docs_for_chashes`` is asked once for the whole batch. Raises :class:`SensitivityUnknown` when the
    catalog cannot be read."""
    wanted = sorted({c for c in chashes if c})
    if not wanted:
        return {}
    try:
        cat = cat if cat is not None else _catalog_reader()
        if cat is None:
            raise RuntimeError("no catalog reader")
        if owners is None:
            found = cat.docs_for_chashes(wanted) or {}
            owners = {c: [str(d) for d in (found.get(c) or [])] for c in wanted}
        docs = sorted({d for c in wanted for d in owners.get(c, ()) if d})
        bad = flagged(docs, cat=cat) if docs else set()
    except SensitivityUnknown:
        raise
    except Exception as exc:  # noqa: BLE001 — see flagged()
        raise SensitivityUnknown(f"{type(exc).__name__}: {exc}"[:300]) from exc
    out: dict[str, list[str]] = {}
    for c in wanted:
        hit = [d for d in owners.get(c, ()) if d in bad]
        if hit:
            out[c] = hit
    return out


def is_sensitive(tumbler: str, *, cat: Any = None) -> bool:
    """True when *tumbler*'s catalog row is sensitive. See :func:`flagged`."""
    return bool(tumbler) and tumbler in flagged([tumbler], cat=cat)


def refuse_if_sensitive(tumbler: str, surface: str, *, cat: Any = None) -> None:
    """Raise :class:`SensitiveDocumentRefused` when *tumbler* is sensitive, after logging the refusal."""
    if is_sensitive(tumbler, cat=cat):
        _log.info("sensitive_document_refused", tumbler=tumbler, surface=surface)
        raise SensitiveDocumentRefused(tumbler, surface)


def tumbler_for_path(
    collection: str,
    source_path: str,
    doc_id_lookup: Callable[[str, str], str] | None = None,
    *,
    cat: Any = None,
) -> str:
    """The tumbler of the document at ``(collection, source_path)``, or ``""`` when the catalog has none.

    The caller's ``doc_id_lookup`` (the queue's own doc id, or the catalog projection the CLI built)
    answers first. With none, or an empty answer, the catalog is asked by path. A lookup that raises
    is :class:`SensitivityUnknown`."""
    try:
        if doc_id_lookup is not None:
            found = doc_id_lookup(collection, source_path) or ""
            if found:
                return str(found)
        cat = cat if cat is not None else _catalog_reader()
        if cat is None:
            return ""
        return str(cat.lookup_doc_id_by_collection_and_path(collection, source_path) or "")
    except Exception as exc:  # noqa: BLE001 — see flagged()
        raise SensitivityUnknown(f"{type(exc).__name__}: {exc}"[:300]) from exc


def sensitive_tumbler_for_uri(uri: str, *, cat: Any = None) -> str:
    """The tumbler of the catalog document whose ``source_uri`` is *uri* when it is sensitive, else ``""``.

    For the source reads of schemes that carry no collection (``file``, ``https``,
    ``x-devonthink-item``). A URI the catalog does not know is not flagged. Raises
    :class:`SensitivityUnknown` when the catalog cannot be read."""
    try:
        cat = cat if cat is not None else _catalog_reader()
        if cat is None:
            raise RuntimeError("no catalog reader")
        entry = cat.by_source_uri(uri)
    except Exception as exc:  # noqa: BLE001 — see flagged()
        raise SensitivityUnknown(f"{type(exc).__name__}: {exc}"[:300]) from exc
    if entry is not None and _entry_flag(entry):
        return str(getattr(entry, "tumbler", "") or "")
    return ""


def sensitive_chashes(collection: str, *, cat: Any = None) -> set[str]:
    """The chash of every chunk a SENSITIVE live document of *collection* names in its manifest.

    The taxonomy gates ask which topics hold a sensitive member; topic members are chunk chashes,
    not tumblers. A collection holds few sensitive documents, so this reads the collection's catalog
    listing once and the manifests of the flagged documents only, never the owners of every member.
    A chunk shared with a plain document counts: the shared row cannot be told apart. Raises
    :class:`SensitivityUnknown` when the catalog cannot be read."""
    try:
        cat = cat if cat is not None else _catalog_reader()
        if cat is None:
            raise RuntimeError("no catalog reader")
        tumblers = [str(e.tumbler) for e in cat.list_by_collection(collection, limit=1_000_000) if _entry_flag(e)]
        if not tumblers:
            return set()
        manifests = cat.get_manifests(tumblers)
        return {m.chash for rows in manifests.values() for m in rows if m.collection in (collection, None)}
    except Exception as exc:  # noqa: BLE001 — see flagged()
        raise SensitivityUnknown(f"{type(exc).__name__}: {exc}"[:300]) from exc
