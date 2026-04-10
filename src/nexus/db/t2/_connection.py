# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared SQLite connection wrapper for the T2 domain stores (RDR-063).

Phase 1 artifact: All four domain stores (MemoryStore, PlanLibrary,
CatalogTaxonomy, Telemetry) hold a reference to the SAME
:class:`SharedConnection` instance — one physical ``sqlite3.Connection``
guarded by one ``threading.Lock``. This keeps Phase 1 a pure refactor
(no lock-scope changes, no concurrency regressions relative to the
monolithic :class:`T2Database`).

Phase 2 will promote each store to open its own ``sqlite3.Connection``
and its own ``threading.Lock`` against the same SQLite file. At that
point this module becomes dead code and is removed in the Phase 2
cleanup bead (``nexus-3d3k``).

The dataclass is intentionally minimal and passive: it does NOT open
the connection, initialize the schema, or own the database path. The
facade (:class:`nexus.db.t2.T2Database`) remains responsible for those
concerns in Phase 1 and simply wraps its existing ``self.conn`` +
``self._lock`` into a :class:`SharedConnection` before passing it to
the domain stores.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass


@dataclass(slots=True)
class SharedConnection:
    """Passive container for a SQLite connection and its guarding lock.

    Attributes:
        conn: The ``sqlite3.Connection`` shared by all domain stores in
            Phase 1. Must be opened with ``check_same_thread=False`` by
            the facade.
        lock: The ``threading.Lock`` that serializes access to ``conn``.
            Domain stores MUST acquire this lock around any call that
            touches ``conn``; they MUST NOT open a second cursor while
            holding the lock from another thread.
    """

    conn: sqlite3.Connection
    lock: threading.Lock
