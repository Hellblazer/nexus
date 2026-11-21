# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Lifecycle reads for the client's schema-bypass tests (RDR-229 P2.5, nexus-p2m1x.10).

``db/t3._BYPASS_SCHEMA_PREFIXES`` held two name prefixes and twelve client sites
tested a collection name against both. The ``quarantine-`` half is lifecycle, and
lifecycle is a column: a collection is quarantined when its catalog row's
``lifecycle_state`` says so, whatever its label. This module is the one place that
reads it, so the twelve sites share one rule instead of twelve copies:

* a row that carries a ``lifecycle_state`` decides: ``quarantine`` is quarantine, any
  other state is not, even under a ``quarantine-`` name;
* a name with no row, or a row with no ``lifecycle_state``, keeps the prefix outcome it
  always had (a sibling the engine minted before registration carried the state, or one
  the stats listing could not join to a catalog row).

The ``taxonomy__`` half is a name test (a migration source: no row attribute identifies it), here and nowhere else.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from nexus.catalog.rows.legacy_names import is_taxonomy_collection_name

_log = structlog.get_logger(__name__)

#: The label prefix the engine mints on a quarantine sibling. Read only for a name with no row.
QUARANTINE_NAME_PREFIX: str = "quarantine-"

#: The ``taxonomy__`` half of the old bypass tuple. :func:`bypasses_canonical_schema` tests it through
#: ``legacy_names.is_taxonomy_collection_name`` (RDR-229 P2.2); no row attribute identifies a taxonomy collection.
TAXONOMY_BYPASS_PREFIXES: tuple[str, ...] = ("taxonomy__",)


def is_quarantine_collection(name: str, row: Mapping[str, Any] | str | None = None) -> bool:
    """Whether collection *name* is a quarantine sibling.

    *row* is the collection's catalog row as the caller already holds it: a stats-listing
    row, the client row cache's entry, or a registry row. A non-mapping *row* (a bare name
    from a name-only substrate) carries no state, and counts as no row.
    """
    state = row.get("lifecycle_state") if isinstance(row, Mapping) else None
    if isinstance(state, str) and state:
        return state == "quarantine"
    return name.startswith(QUARANTINE_NAME_PREFIX)


def bypasses_canonical_schema(name: str, row: Mapping[str, Any] | str | None = None) -> bool:
    """Whether *name* is a collection the canonical chunk schema and the drift checks skip:
    a ``taxonomy__`` collection (by name until P2.9) or a quarantine sibling (by row)."""
    return is_taxonomy_collection_name(name) or is_quarantine_collection(name, row)


def registry_rows_by_name(cat: Any) -> dict[str, dict]:
    """The catalog registry's rows keyed by name, or ``{}`` when they cannot be read.

    For a caller whose names are not in the vector listing, so no stats row carries their
    state: the registry is the only row it has. An unreadable registry is no row, and the
    name decides, as it did before this read existed; the failure is logged, not hidden.
    """
    try:
        return {r["name"]: r for r in cat.list_collections() if isinstance(r, Mapping) and r.get("name")}
    except Exception as exc:  # noqa: BLE001 — a registry read that fails leaves the prefix outcome in force, reported below
        _log.warning("collection_registry_unreadable_for_lifecycle", error=str(exc))
        return {}
