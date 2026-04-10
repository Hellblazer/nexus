# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""PlanLibrary — reusable query-plan storage (RDR-063 Phase 1).

This module is a placeholder stub. The actual extraction of the
``plans`` / ``plans_fts`` schema and methods (``save_plan``,
``search_plans``, ``list_plans``) from :mod:`nexus.db.t2` happens in
bead ``nexus-kpe7`` (Phase 1 step 3).

Note: ``nexus-kpe7`` also fixes Landmine 1 — the hidden coupling in
``src/nexus/commands/catalog.py:93`` where ``_seed_plan_templates``
calls ``db.conn.execute(...)`` directly. The extraction adds a
``plan_exists(query, tag)`` method on the facade / store and rewrites
the call site to go through it, so Phase 2's per-store connection
split does not break builtin-template seeding.

Until extraction, the monolithic :class:`nexus.db.t2.T2Database` owns
all plan operations directly.
"""

from __future__ import annotations

# Per-domain migration guard (RDR-063 Open Question 3).
_migrated_paths: set[str] = set()
