# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Legacy ``nexus.catalog.catalog_db`` import surface.

RDR-120 P5.A.2 (nexus-2t7o5): the catalog SQLite layer moved into T2
as the eighth domain store. ``CatalogDB`` is now an alias for
:class:`nexus.db.t2.catalog.CatalogStore`; this module exists only as
a re-export shim so existing ``from nexus.catalog.catalog_db import
CatalogDB`` imports continue to resolve unchanged.

No ``sqlite3.connect`` lives here — see :mod:`nexus.db.t2.catalog`
for the authoritative implementation. P5.A.3 will remove the
remaining direct catalog/ sqlite call sites and drop the catalog-
allowlist count to zero.
"""
from __future__ import annotations

from nexus.db.t2.catalog import (  # noqa: F401 — re-exports
    CatalogStore as CatalogDB,
    _DOCUMENTS_FTS_TRIGGERS,
    _SCHEMA_SQL,
)

__all__ = ("CatalogDB", "_DOCUMENTS_FTS_TRIGGERS", "_SCHEMA_SQL")
