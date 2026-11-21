# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The owner an ``.nxexp`` import registers its documents under (RDR-229, nexus-wbfpw.31/.33).

A collection's owner is its SOURCE, set once at registration: the repo owner for a repo's collections,
the corpus curator for a doc corpus, the knowledge curator for knowledge (Sam, 2026-11-21, T2
``nexus/sam-ruling-rdr229-owner-is-source-2026-11-21``). The bulk import used to put every imported
document of a collection with no usable row under the knowledge curator, so catalog-044 then derived the
curator as the owner of a REPO's collections (gate-xr789: ``code__arcaneum-2ad2825c``).

:func:`resolve_import_owner_tumbler` reads the collection's catalog ROW first, never its name (RDR-204):
an owner segment is not always tumbler-derived, and a name with a non-canonical model segment does not
even parse. Only when the row names no registered owner and the collection holds no live document does it
derive the source, in this order, never refusing a collection (stranding user data is forbidden):

1. the bundle's ``owner`` header (an export since RDR-229 records the repo owner's ``repo_hash`` and
   ``repo_root``): the existing owner with that ``repo_hash``, else a new repo owner with an empty
   ``repo_root`` (the header's root is the source machine's path, so the first ``nx index repo`` here
   backfills the real one; the header keeps ``repo_root`` as information only);
2. a collection name of the repo-synth stem shape ``<slug>-<hash8>`` (``code__arcaneum-2ad2825c__...``)
   whose collection is a ``code`` one, or a ``docs``/``rdr`` one when the target catalog already holds
   that repo's owner (a repo owner with ``repo_hash`` ``<hash8>``, from an earlier ``nx index repo`` or a
   ``code`` import) or a ``code`` collection of the same stem: the repo owner with ``repo_hash``
   ``<hash8>``, existing or registered (empty ``repo_root``: the source machine's path is not this one's);
3. a ``docs`` corpus with neither: its corpus curator, named the way the doc indexer names it (the stem
   is the corpus). A hash-suffixed stem here looks like a repo's collection that came before its ``code``
   one, so the import says so once (``on_hint``, the structlog warning) and the collection stays with the
   curator, because an existing owner is never moved by a later import. A tumbler-shaped stem
   (``docs__1-14__...``) is an owner id, not a corpus name, so it has no corpus curator and goes to 4;
4. the knowledge curator, with a loud warning naming the collection and the reason, and the caller's
   *on_fallback* hook told so the import summary can count it.

A knowledge collection keeps its one curator.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import structlog

from nexus.catalog.rows.legacy_names import owner_stem
from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger(__name__)

#: An owner TUMBLER in a name's owner segment (``docs__1-14__...``, the hyphenated form): not a corpus name.
_TUMBLER_STEM = re.compile(r"^[0-9]+(-[0-9]+)*$")


def _repo_owner(reader: Any, writer: Any, repo_hash: str, *, root: str, name: str) -> Tumbler:
    """The repo owner holding *repo_hash*, else a newly registered one. ``register_owner`` converges on an
    existing owner of the same ``(name, owner_type)`` and rewrites its ``repo_hash``, so a name another
    repo owner already holds is made unique with the hash."""
    existing = reader.owner_for_repo(repo_hash)
    if existing is not None:
        return existing
    taken = {str(o.get("name") or "") for o in reader.list_owners_by_type("repo", include_deactivated=True)}
    owner_name = name if name and name not in taken else f"{name or 'repo'}-{repo_hash}"
    return writer.register_owner(owner_name, "repo", repo_hash=repo_hash, repo_root=root or None)


def _row_owner(collection_name: str, row: dict, reader: Any) -> Tumbler | None:
    """The owner the collection already has: its row's ``owner_id`` when that is a registered owner, else
    the owner of a live document in it. Writers store the id in the name's hyphenated form (``1-1``), and
    before nexus-7tys2 ``upsertCollection`` overwrote it from the name on every chunk write, so both the
    stored value and its hyphens-as-dots form are tried, and either counts only when the catalog confirms
    it is a registered owner. A slug (``arcaneum-2ad2825c``) matches no owner and falls through."""
    owner_id = str(row.get("owner_id") or "")
    for candidate in dict.fromkeys((owner_id, owner_id.replace("-", "."))):
        if candidate and reader.get_owner_by_prefix(candidate) is not None:
            try:
                return Tumbler.parse(candidate)
            except Exception:  # noqa: BLE001 — an unparseable owner id falls through
                _log.warning("import_owner_row_unparseable", collection=collection_name, owner_id=candidate)
    existing = reader.list_by_collection(collection_name, limit=1)
    if existing:
        return existing[0].tumbler.owner_address()
    return None


def resolve_import_owner_tumbler(
    collection_name: str, reader: Any, writer: Any, *,
    bundle_owner: dict | None = None,
    content_type: str = "",
    on_fallback: Callable[[str], None] | None = None,
    on_hint: Callable[[str], None] | None = None,
) -> Tumbler:
    """The owner an import-minted document of *collection_name* is registered under (module docstring).

    *bundle_owner* is the export header's ``owner`` (``{"repo_hash", "repo_root", "name"}``), or ``None``.
    *content_type* is the type the bundle declares, used only when the collection has no catalog row.
    *on_fallback* receives the reason when the knowledge curator is taken because nothing else applied.
    *on_hint* receives the hint when a hash-suffixed ``docs`` stem stays with its corpus curator."""
    row = reader.get_collection(collection_name) or {}
    ctype = str(row.get("content_type") or content_type or "")
    if ctype == "knowledge":
        return _knowledge_curator(reader, writer)
    current = _row_owner(collection_name, row, reader)
    if current is not None:
        return current
    stem, stem_parts = owner_stem(collection_name)
    repo_hash = str((bundle_owner or {}).get("repo_hash") or "")
    if repo_hash:                                                           # (1) the bundle's owner
        slug = str((bundle_owner or {}).get("name") or "") or (stem_parts[0] if stem_parts else "")
        # root="": the header's repo_root is the source machine's path; backfill fills in this machine's
        return _repo_owner(reader, writer, repo_hash, root="", name=slug)
    if stem_parts is not None and (ctype == "code" or (
        ctype in ("docs", "rdr") and (                                      # the repo is already known here
            reader.owner_for_repo(stem_parts[1]) is not None
            or any(owner_stem(str(c.get("name") or ""))[1] == stem_parts
                   for c in reader.list_collections(lifecycle_state="live", content_type="code"))
        )
    )):
        return _repo_owner(reader, writer, stem_parts[1], root="", name=stem_parts[0])   # (2) the stem
    if ctype == "docs":                                                     # (3) the corpus curator
        if stem and not _TUMBLER_STEM.match(stem):
            if stem_parts is not None:
                hint = (f"{stem} looks like a repo's collection; it was given its corpus curator as owner. "
                        f"To give it the repo's owner, import that repo's code__ bundle, then remove this "
                        f"collection and import it again.")
                _log.warning("import_docs_stem_looks_like_a_repo", collection=collection_name, hint=hint)
                if on_hint is not None:
                    on_hint(hint)
            owner_t = reader.curator_owner_tumbler_by_name(stem)
            return owner_t if owner_t is not None else writer.register_owner(stem, "curator")
    reason = (
        f"no registered owner on its row, no live document, no bundle owner, and "
        f"{'no repo stem' if stem_parts is None else 'a repo stem but no repo owner or code collection of it'} "
        f"(content type {ctype or 'unknown'})"
    )
    _log.warning("import_owner_fell_back_to_knowledge_curator", collection=collection_name, reason=reason)
    if on_fallback is not None:
        on_fallback(reason)
    return _knowledge_curator(reader, writer)


def _knowledge_curator(reader: Any, writer: Any) -> Tumbler:
    """The ``knowledge`` curator, the owner ``catalog_store_hook_tracked`` registers every note under."""
    owner_t = reader.curator_owner_tumbler_by_name("knowledge")
    return owner_t if owner_t is not None else writer.register_owner("knowledge", "curator")


def export_owner_header(collection_name: str, reader: Any) -> dict | None:
    """The ``owner`` key an export writes into its header: ``{"name", "repo_hash", "repo_root"}`` of the
    repo owner the collection's row names, or ``None`` for any other owner (a curator has no repo) or an
    unreadable one. An importer without the key reads the file as it always did (an unknown header key is
    ignored), so ``FORMAT_VERSION`` is unchanged. Best effort: the header is metadata, and an export must
    not fail for want of it."""
    try:
        row = reader.get_collection(collection_name) or {}
        owner_id = str(row.get("owner_id") or "")
        for candidate in dict.fromkeys((owner_id, owner_id.replace("-", "."))):
            owner = reader.get_owner_by_prefix(candidate) if candidate else None
            if owner is not None:
                if owner.get("owner_type") == "repo" and owner.get("repo_hash"):
                    return {
                        "name": str(owner.get("name") or ""),
                        "repo_hash": str(owner["repo_hash"]),
                        "repo_root": str(owner.get("repo_root") or ""),
                    }
                return None
    except Exception as exc:  # noqa: BLE001 — best effort: see the docstring
        _log.warning("export_owner_header_unreadable", collection=collection_name, error=str(exc))
    return None
