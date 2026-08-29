# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared T3-orphan-collection classification (nexus-8tnz2).

A T3 collection with chunks but ZERO LIVE catalog documents referencing it
is benchmark/gate debris (T2 ``nexus/catalog-cleanup-2026-08-03-executed-
and-prevention`` [21385] item 3): no producer in this repo creates a
collection named e.g. ``code__test-repo-<hex>__voyage-code-3__v1`` on
purpose, and nothing at write time keeps such collections out of the
production tenant.

ONE definition, three consumers (nexus-8tnz2 design-of-record locked
invariant: "Classification has ONE definition; doctor, verify, and the arm
agree by construction"):

  - ``nx catalog doctor --t3-vs-catalog`` (the ``t3_orphans`` field,
    originally nexus-6dan)
  - ``nx catalog reconcile-stale --execute drop-orphan-collections`` (the
    sweep arm)
  - ``nx catalog verify``'s report (the ``orphan_collections`` field)

All three call :func:`classify_t3_orphan_collections` directly.

nexus-8tnz2 fix-round CRITICAL 2: ``cat.collection_doc_counts()`` (the
default, LIVE-only count) reads the tombstone-aware ``collection_doc_counts``
security_invoker view (``deleted_at IS NULL``, catalog-019-tombstone-aware-
read-views.xml) -- a collection whose catalog documents are ALL
soft-tombstoned (still restorable until ``purge_trash``, RDR-156 D6) is
therefore indistinguishable, via that count alone, from a collection that
never had a catalog document registered at all. Hard-deleting the former
would destroy still-recoverable content. Each zero-live-doc,
chunks-present row is therefore split by a SECOND, ``include_deleted=True``
read (all catalog docs, live+tombstoned, for the same collection) into
exactly two classes -- every row this function returns (other than an
unreadable-count ``error`` row) carries one:

  ``"orphan"``           live docs == 0 AND all docs == 0. No catalog
                          document was ever registered for this T3
                          collection. The ONLY class
                          ``drop-orphan-collections`` may hard-delete.
  ``"tombstoned-only"``  live docs == 0 AND all docs > 0. Every catalog
                          document for this collection is soft-deleted,
                          not gone -- reported (with ``tombstoned_count``),
                          NEVER a delete target.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader  # noqa: F401 — PEP 563 deferred annotation use


def classify_t3_orphan_collections(cat: "CatalogReader", t3_db: Any) -> list[dict]:
    """T3 collections with chunks but zero LIVE catalog documents.

    Returns one dict per candidate collection:

      - ``{"name": str, "chunk_count": int, "class": "orphan"}`` -- no
        catalog document, live or tombstoned, ever referenced this
        collection. Safe to hard-delete.
      - ``{"name": str, "chunk_count": int, "class": "tombstoned-only",
        "tombstoned_count": int}`` -- every referencing catalog document
        is soft-deleted (restorable until ``purge_trash``). NEVER a delete
        target for any caller.
      - ``{"name": str, "error": str}`` when the per-collection T3 chunk
        count could not be read. A read failure is reported, never
        silently collapsed to ``count=0`` -- the nexus-pyv0e class of bug
        (a swallowed exception reading as a false PASS with zero detection
        capability).

    Bypass-schema collections (``taxonomy__*``) never carry chunked text
    and are excluded, mirroring the original nexus-6dan
    ``--t3-vs-catalog`` check. ``t3_db.list_collections()`` and BOTH
    ``cat.collection_doc_counts()`` calls (live-only, then
    ``include_deleted=True``) raise uncaught: an unreadable T3 collection
    listing, or an unreadable doc-count of EITHER kind, is the caller's
    INCOMPLETE condition to refuse on, not this function's to swallow into
    an empty (falsely clean) orphan list, or to guess around by dropping a
    collection that might actually be tombstoned-only.
    """
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415 — command-local import (nexus.db.t3)

    t3_names = {
        c["name"] for c in t3_db.list_collections()
        if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
    }
    docs_per_coll: dict[str, int] = cat.collection_doc_counts()
    docs_per_coll_all: dict[str, int] = cat.collection_doc_counts(include_deleted=True)

    orphans: list[dict] = []
    for name in sorted(t3_names):
        if docs_per_coll.get(name, 0) > 0:
            continue
        # Only flag if the T3 collection actually has chunks; an empty T3
        # collection with no docs is a "zombie", a different class entirely
        # (see doctor.py's ``_run_t3_vs_catalog``).
        try:
            col = t3_db.get_collection(name=name)
            count = col.count()
        except Exception as exc:  # noqa: BLE001 — boundary catch; recorded, never swallowed (nexus-pyv0e class)
            orphans.append({"name": name, "error": str(exc)})
            continue
        if count <= 0:
            continue
        tombstoned_count = docs_per_coll_all.get(name, 0)
        if tombstoned_count > 0:
            orphans.append({
                "name": name, "chunk_count": count,
                "class": "tombstoned-only", "tombstoned_count": tombstoned_count,
            })
        else:
            orphans.append({"name": name, "chunk_count": count, "class": "orphan"})
    return orphans
