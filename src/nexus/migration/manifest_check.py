# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P3.M (nexus-ue6g7.21): the manifest-orphans validation leg.

Builds the ``manifest_orphan_check`` callable the P3 validation gate consumes,
wrapping the P-1b (nexus-avjdd) catalog-client callables ``manifest_backfill``
and ``manifest_orphans``.

The production-incident class this guards (RDR-159 §gap #2 / RF-3): the manifest
check reads the MIGRATED PG catalog, so it is non-vacuous ONLY after T2
``migrate all`` has populated the catalog tables. Running it on an empty catalog
returns a false-clean zero — the original production run "passed" manifest
validation that way without ever migrating anything. The check therefore probes
the migrated catalog FIRST and raises :class:`ValidationCheckVacuous` (a loud
block, never a silent pass) when it is empty.

Sequencing is locked by P-1b: ``manifest_backfill`` MUST run before
``manifest_orphans`` (pre-backfill NULL-collection rows would read as false
orphans). The orphan count is summed across the dim tables.
"""
from __future__ import annotations

from typing import Any, Callable

import structlog

from nexus.migration.validation import ValidationCheckVacuous

_log = structlog.get_logger(__name__)

#: The migrated-catalog documents relation (orchestrator ``_RELATION_MAP``); its
#: tenant-scoped row count is the "did T2 populate the catalog" probe.
_CATALOG_DOCS_RELATION: str = "nexus.catalog_documents"

#: The pgvector chunk-table dims the manifest stored function accepts.
_MANIFEST_DIMS: tuple[int, ...] = (384, 768, 1024)


def build_manifest_orphan_check(
    catalog_client: Any, *, dims: tuple[int, ...] = _MANIFEST_DIMS
) -> Callable[[], int]:
    """Return a ``() -> int`` orphan-count check for the P3 validation gate.

    The returned callable:

    1. probes ``relation_counts([nexus.catalog_documents])`` — an empty migrated
       catalog means T2 has not run, so it raises :class:`ValidationCheckVacuous`
       (the gate turns that into a loud block) rather than a false-clean zero;
    2. calls ``manifest_backfill`` FIRST (locked P-1b order — pre-backfill
       NULL-collection rows read as false orphans);
    3. sums ``manifest_orphans(dim)["count"]`` across ``dims`` and returns it
       (zero is clean; > 0 BLOCKS unlock).
    """

    def _check() -> int:
        counts = catalog_client.relation_counts([_CATALOG_DOCS_RELATION])
        doc_count = counts.get(_CATALOG_DOCS_RELATION)
        if not doc_count:
            # 0 rows, or the relation absent (not whitelisted / unreachable):
            # either way the orphan check cannot be non-vacuous here.
            raise ValidationCheckVacuous(
                "manifest-orphan check is vacuous — the migrated catalog "
                f"({_CATALOG_DOCS_RELATION}) is empty, so T2 migrate-all has "
                "not populated it. A clean orphan count here would be a false "
                "pass; run T2 first and re-validate."
            )
        # Backfill BEFORE orphans (P-1b sequencing).
        stamped = catalog_client.manifest_backfill()
        _log.info("manifest_backfill_done", stamped=stamped)
        total = 0
        for dim in dims:
            result = catalog_client.manifest_orphans(dim)
            total += int(result["count"])
        _log.info("manifest_orphans_total", count=total, dims=list(dims))
        return total

    return _check
