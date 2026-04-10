# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Telemetry — search relevance log (RDR-063 Phase 1).

This module is a placeholder stub. The actual extraction of the
``relevance_log`` schema and methods (``log_relevance``,
``log_relevance_batch``, ``get_relevance_log``,
``expire_relevance_log``) from :mod:`nexus.db.t2` happens in bead
``nexus-yjww`` (Phase 1 step 6).

Note: after extraction the facade's ``T2Database.expire()`` must call
BOTH ``memory_store.expire()`` AND ``telemetry.expire()`` in sequence —
otherwise telemetry silently accumulates. Contract enforced by
``tests/test_t2_facade.py::test_expire_calls_all_domains``.

Until extraction, the monolithic :class:`nexus.db.t2.T2Database` owns
all relevance-log operations directly.
"""

from __future__ import annotations

# Per-domain migration guard (RDR-063 Open Question 3).
_migrated_paths: set[str] = set()
