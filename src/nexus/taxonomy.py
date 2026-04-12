# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Deprecation shim — taxonomy moved to nexus.db.t2.catalog_taxonomy.

RDR-063 Phase 1 step 4 (bead ``nexus-u29l``) extracted the topic
taxonomy implementation out of this module into the new
:mod:`nexus.db.t2.catalog_taxonomy` package, where it lives as a
:class:`CatalogTaxonomy` class with its own dedicated
``sqlite3.Connection`` and ``threading.Lock`` (promoted to an
independent connection in Phase 2, bead ``nexus-3d3k``). The old
module-level functions reached through the monolithic ``T2Database``'s
lock and connection directly, which defeated the goal of giving the
taxonomy domain its own connection.

This file remains as a thin compatibility shim so existing import
sites continue to work without modification:

  * ``from nexus.taxonomy import get_topics, get_topic_tree, ...``
    (used by ``tests/test_taxonomy.py`` and ``commands/taxonomy_cmd.py``)
  * ``import nexus.taxonomy as tax`` followed by ``tax.cluster_and_persist(...)``
    (used by two tests in ``tests/test_taxonomy.py``)

Each wrapper accepts a :class:`T2Database` and forwards to the
matching method on ``db.taxonomy``. The wrappers contain no logic of
their own.

**Removal**: per RDR-063 §Open Question 1 / Phase 1 Step 4, this shim
is removed in the first PR after Phase 2 (bead ``nexus-3d3k``) merges.
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


def cluster_and_persist(
    db: "T2Database",
    project: str,
    *,
    k: int | None = None,
) -> int:
    """Deprecated wrapper — use ``db.taxonomy.cluster_and_persist(...)``."""
    return db.taxonomy.cluster_and_persist(project, k=k)


def rebuild_taxonomy(
    db: "T2Database",
    project: str,
    *,
    k: int | None = None,
) -> int:
    """Deprecated wrapper — use ``db.taxonomy.rebuild_taxonomy(...)``."""
    return db.taxonomy.rebuild_taxonomy(project, k=k)
