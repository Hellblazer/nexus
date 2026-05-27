# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared T2 connection-tuning constants (RDR-129 B1, nexus-qi1zb).

Leaf module: no intra-``nexus.db.t2`` imports, so every domain store and the
package facade can import it without a cycle.
"""
from __future__ import annotations

#: ``busy_timeout`` for every daemon-owned domain-store serving connection.
#: 30s matches the startup-migration window
#: (:data:`nexus.db.t2._BOOTSTRAP_BUSY_TIMEOUT_MS`) and absorbs the cross-store
#: WAL contention that otherwise drops best-effort writes under heavy
#: concurrent indexing (RDR-129 RF-B1). The prior 5s was falsified by two
#: production shakeouts. Paired with the daemon ``_dispatch`` lock-retry (B2)
#: so a contention window longer than the timeout still becomes a wait, not a
#: drop. NB: connections that must fail fast (memory_store's best-effort
#: access-count update, ``_ACCESS_TRACK_BUSY_MS = 0``) intentionally do NOT use
#: this value.
SERVING_BUSY_TIMEOUT_MS: int = 30000
