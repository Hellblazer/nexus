# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""CatalogTaxonomy — topics + topic_assignments (RDR-063 Phase 1).

This module is a placeholder stub. The actual migration of
:mod:`nexus.taxonomy` (7 top-level functions, 26 internal T2 refs — see
RF-063-2 plus the auditor's correction adding ``rebuild_taxonomy`` to
the list) into a proper ``CatalogTaxonomy`` class happens in bead
``nexus-u29l`` (Phase 1 steps 4-5). That bead is tagged NON-MECHANICAL
(~3-4h) because:

1. ``taxonomy.py`` reaches through ``db._lock`` and ``db.conn.execute``
   directly — every call site must be rewritten onto this module's
   connection instead of the monolithic T2 lock.
2. ``get_topic_tree()`` at line 134 re-enters the lock inside a nested
   traversal; transaction-scope review is required (not a mechanical
   rewrite).
3. The Known Defect in ``get_topic_docs()`` (JOIN across
   ``topic_assignments.doc_id = memory.title`` with
   ``project = topics.collection``) is documented, not fixed — per
   RDR-063 Open Question 1 resolution (Option 3: document T2-only
   scope).

After ``nexus-u29l`` lands, ``src/nexus/taxonomy.py`` becomes a
deprecation shim that re-exports from this module. The shim is removed
in the first PR after Phase 2 merges.
"""

from __future__ import annotations

# Per-domain migration guard (RDR-063 Open Question 3).
_migrated_paths: set[str] = set()
