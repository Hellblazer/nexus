# SPDX-License-Identifier: AGPL-3.0-or-later
"""The link-graph audit, moved out of ``http_catalog_client`` (RDR-171 Step 0).

``HttpCatalogClient.link_audit`` delegates here; RDR-171 Step 5 adds the
stale dchash report to it.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.catalog.http_catalog_client import HttpCatalogClient


def link_audit(client: HttpCatalogClient, *, t3: Any = None) -> dict:
    """Audit the link graph: totals, per-type/creator breakdown, orphans, dupes.

    nexus-ai41v: this used to ``return {}`` — "not supported in initial
    service-mode implementation". Service mode is now EVERY mode, and the
    verb ships on two surfaces (``nx catalog link-audit`` and the MCP
    ``catalog_link_audit`` tool), so an empty dict was not a graceful
    degradation: ``--json`` printed ``{}``, which reads as a CLEAN audit.
    That is the silent-false-clean shape this project has been burned by
    before (nexus-ou4tb folded unreadable collections in as "no ghosts").

    Everything here is computed from reads the engine already serves — the
    paginated ``/link_query`` with no filters, and ``/links/orphaned``
    (nexus-ysrwi) — so no engine change is required.

    ``t3`` is accepted and ignored: the local implementation used it for a
    stale-chash cross-check against T3, which this substrate answers from
    the manifest instead. It stays in the signature for call-site parity.
    """
    by_type: dict[str, int] = defaultdict(int)
    by_creator: dict[str, int] = defaultdict(int)
    seen: dict[tuple[str, str, str], int] = defaultdict(int)
    total = 0

    page = 200
    offset = 0
    while True:
        batch = client.link_query(limit=page, offset=offset)
        if not batch:
            break
        for lk in batch:
            total += 1
            by_type[lk.link_type] += 1
            by_creator[lk.created_by or ""] += 1
            seen[(str(lk.from_tumbler), str(lk.to_tumbler), lk.link_type)] += 1
        if len(batch) < page:
            break
        offset += page

    duplicates = {k: n for k, n in seen.items() if n > 1}
    result = client._get("/links/orphaned")
    orphaned = result.get("links", []) if result else []

    return {
        "total": total,
        "by_type": dict(by_type),
        "by_creator": dict(by_creator),
        "duplicate_count": len(duplicates),
        "duplicates": [
            {"from_tumbler": f, "to_tumbler": t, "link_type": lt, "count": n}
            for (f, t, lt), n in sorted(duplicates.items())
        ],
        "orphaned_count": len(orphaned),
        "orphaned": orphaned,
    }
