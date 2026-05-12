# SPDX-License-Identifier: AGPL-3.0-or-later
"""Knowledge-entry catalog registration helper.

nexus-8g79.10 (V1): hosted at this lower layer so MCP infra
(``mcp/core.py``) and CLI command modules can both invoke without
the MCP layer reaching up into the CLI presentation layer.
Previously this function lived in ``commands/store.py`` and was
imported FROM ``mcp/core.py:1029`` — a layering inversion flagged
by the post-4.32.4 multi-agent audit.

Callers: ``mcp/core.py`` (MCP ``store_put`` tool),
``commands/store.py`` (``nx store put`` CLI),
``commands/memory.py`` (``nx memory promote`` CLI).
"""
from __future__ import annotations

import structlog

_log = structlog.get_logger(__name__)


def catalog_store_hook(
    title: str, doc_id: str, collection_name: str,
) -> str:
    """Register a knowledge entry in the catalog. Silently skipped if absent.

    Returns the catalog ``Document.doc_id`` (Tumbler string) so the
    caller can pass it to ``T3Database.put()`` as ``catalog_doc_id``
    for chunk-write-time embedding (RDR-101 Phase 3 PR δ Stage B.4).
    Returns ``""`` when the catalog is absent or an error occurs —
    the schema funnel drops empty ``doc_id`` at the boundary.

    ``doc_id`` here is the T3 chunk natural-id (RDR-108 D1 / nexus-kmb6:
    ``sha256(content)[:32]``). It is consulted for legacy
    ``meta.doc_id`` dedup via ``cat.by_doc_id``: catalog entries
    written before Phase 4 stored the legacy 16-char sha256-of-
    collection-and-title under ``meta.doc_id``, so this lookup misses
    on those legacy entries and the hook re-registers. That is the
    intentional behavior for the upgrade window; a follow-up catalog
    GC pass consolidates duplicates once all callers have been
    updated.
    """
    try:
        from nexus.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            return ""

        cat = Catalog(cat_path, cat_path / ".catalog.db")

        # Dedup by chunk_chroma_id stored in legacy meta.doc_id.
        existing = cat.by_doc_id(doc_id)
        if existing is not None:
            return str(existing.tumbler)

        # Get or create "knowledge" curator owner. Filter on owner_type
        # so a same-named REPO owner cannot shadow the intended curator
        # (same bug shape as the doc_indexer family fix).
        rows = cat._db.execute(
            "SELECT tumbler_prefix FROM owners WHERE name = 'knowledge' "
            "AND owner_type = 'curator'"
        ).fetchone()
        if rows:
            from nexus.catalog.tumbler import Tumbler
            owner = Tumbler.parse(rows[0])
        else:
            owner = cat.register_owner("knowledge", "curator")

        tumbler = cat.register(
            owner=owner, title=title, content_type="knowledge",
            physical_collection=collection_name,
            meta={"doc_id": doc_id},
        )
        return str(tumbler)
    except Exception:
        _log.debug("catalog_store_hook_failed", exc_info=True)
        return ""
