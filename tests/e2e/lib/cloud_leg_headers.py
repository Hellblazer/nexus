# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which cloud-leg step carries each ``X-Nexus-*`` header through the edge (nexus-srl9e). Data only; the census is
tests/scripts/test_cloud_leg_header_census.py."""
from __future__ import annotations

#: Every ``X-Nexus-*`` header the client names in a string literal under src/ -> the fixed-core step whose client call
#: carries it through the edge, or ``"exempt: <reason>"`` (nexus-srl9e). The leg's ledger mapping (cloud_leg.ROUTE_MAP) sees only routes, so a
#: header added on an existing route ships with no ledger entry; X-Nexus-Identity-Token (stripped by the CDN in 8.5.0)
#: and X-Nexus-Part-Sha256 (conexus #710) were both of that kind. tests/scripts/test_cloud_leg_header_census.py fails
#: on a header in src missing here, on one here missing from src, and on a step that is not a core step of the plan.
#: The backup routes run in engine-release Step 6.2 (the post-deploy `nx backup` round trip through the edge, an engine
#: cut only); a client-only release that changes them has no cloud gate for these headers.
_BACKUP_EXPORT = ("exempt: response headers of `nx backup`'s managed-mode export pages, a whole-tenant read the leg does "
                  "not drive; engine-release Step 6.2 drives them")

HEADER_COVERAGE: dict[str, str] = {
    # RefreshableHttpStoreMixin._build_headers: X-Nexus-Tenant plus client_identity_headers() on every T2 request
    "X-Nexus-Tenant": "memory-put-threads-identity",
    "X-Nexus-Client-Version": "memory-put-threads-identity",
    "X-Nexus-T1-Session": "scratch-put",                 # HttpScratchStore's per-session token header
    "X-Nexus-Identity-Token": "memory-put-threads-identity",
    "X-Nexus-Request-Deadline-Ms": "exempt: sent only on the vectors chunk-upsert route (the indexers, collection "
                                   "reembed, the exporter's import); a store put writes through "
                                   "/v1/catalog/manifest/write_many and no core step indexes",
    "X-Nexus-Deadline-Outcome": "exempt: a response header the engine sends only on a 503 that refuses or aborts a "
                                "request at its deadline, which the leg cannot provoke",
    "X-Nexus-Skipped-Collections": "exempt: a response header the engine sends only when a fan-out read skips a "
                                   "collection, which depends on tenant state the leg does not set up",
    "X-Nexus-Export-Done": _BACKUP_EXPORT,
    "X-Nexus-Export-Last-Key": _BACKUP_EXPORT,
    "X-Nexus-Export-Rows": _BACKUP_EXPORT,
    "X-Nexus-Export-Sha256": _BACKUP_EXPORT,
    "X-Nexus-Export-Table": _BACKUP_EXPORT,
    "X-Nexus-Export-Table-Rows": _BACKUP_EXPORT,
    "X-Nexus-Part-Sha256": "exempt: `nx backup restore` (a tenant import) loads a whole store into a tenant, which "
                           "the leg does not drive; engine-release Step 6.2 drives it (the edge forwards it since conexus #710)",
}
