# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MemoryStore — agent memory table (RDR-063 Phase 1).

This module is a placeholder stub. The actual extraction of the
``memory`` / ``memory_fts`` schema and methods (``put``, ``get``,
``search``, ``list_entries``, ``delete``, ``expire``,
``find_overlapping_memories``, ``merge_memories``,
``flag_stale_memories``) from :mod:`nexus.db.t2` happens in the next
bead: ``nexus-vx3c`` (Phase 1 step 2).

Until then, the monolithic :class:`nexus.db.t2.T2Database` owns all
memory operations directly.
"""

from __future__ import annotations

# Per-domain migration guard (RDR-063 Open Question 3): each domain
# module owns its own guard set so adding a migration to one domain
# does not re-probe unrelated domains. Populated by the real extraction
# bead.
_migrated_paths: set[str] = set()
