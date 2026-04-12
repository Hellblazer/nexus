# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Deprecation shim — taxonomy moved to nexus.db.t2.catalog_taxonomy.

Thin compatibility shim so existing import sites (tests, CLI commands)
continue to work without modification. Each wrapper accepts a
:class:`T2Database` and forwards to ``db.taxonomy``.

RDR-070 (nexus-9k5): ``cluster_and_persist`` removed — replaced by
``discover_topics`` on :class:`CatalogTaxonomy`. ``rebuild_taxonomy``
signature changed to accept embeddings + ChromaDB client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.db.t2 import T2Database


def get_topics(
    db: "T2Database",
    *,
    parent_id: int | None = None,
) -> list[dict[str, Any]]:
    """Deprecated wrapper — use ``db.taxonomy.get_topics(parent_id=...)``."""
    return db.taxonomy.get_topics(parent_id=parent_id)


def assign_topic(db: "T2Database", doc_id: str, topic_id: int) -> None:
    """Deprecated wrapper — use ``db.taxonomy.assign_topic(...)``."""
    db.taxonomy.assign_topic(doc_id, topic_id)


def get_topic_docs(
    db: "T2Database",
    topic_id: int,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Deprecated wrapper — use ``db.taxonomy.get_topic_docs(...)``."""
    return db.taxonomy.get_topic_docs(topic_id, limit=limit)


def get_topic_tree(
    db: "T2Database",
    collection: str = "",
    *,
    max_depth: int = 2,
) -> list[dict[str, Any]]:
    """Deprecated wrapper — use ``db.taxonomy.get_topic_tree(...)``."""
    return db.taxonomy.get_topic_tree(collection, max_depth=max_depth)
