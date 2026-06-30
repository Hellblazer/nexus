# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 2 (Gap 4) — migration route-coverage go-live gate.

A managed migration only works if the edge (nginx/WAF) lets every migration
endpoint through to the service for a tenant bearer. The 6.0.0 dogfood found the
T2 import routes 403'd at the WAF: a migration that passed locally was silently
blocked at the edge. This module is the single source of truth for the migration
endpoint allowlist plus a go-live gate that probes each route and FAILS on any
the edge rejects.

Edge-blocked vs app-reached
---------------------------
A route is *reachable* when a request gets PAST the edge to the application —
even a ``400``/``422`` (the app received the probe and rejected its body) counts.
Only ``403`` (WAF/authz block) and ``404`` (route not proxied) mean the edge
never forwarded the request. The gate therefore treats ``{403, 404}`` as
unreachable and everything else as reachable.

Run against the live cloud edge with a real authenticated HTTP probe, this is the
xr7.8.9-style go-live check (conexus-side; the probe is injected so the gate
logic is unit-testable without a network).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

#: The canonical migration-path edge allowlist (RDR-176 Research-2 §D), expanded
#: from the path groups. This is the SINGLE SOURCE OF TRUTH: the edge allowlist
#: (nginx) and the go-live gate both derive from it, so a route the migration
#: path needs cannot ship without edge coverage.
#:
#: Scope note: this is the set of routes the managed edge must PERMIT for the
#: migration path, per RDR §D — not strictly "routes the ETL POSTs". It is a
#: deliberate superset on two axes, because for a go-live SAFETY gate an
#: over-inclusive false-alarm (a permitted-but-unused route the edge blocks) is
#: far safer than an under-inclusive silent edge-block of a real migration route:
#:   - ``/v1/chash/upsert_many`` is in §D though the current chash ETL writes via
#:     ``/v1/chash/import`` (chash_etl.py — chosen to preserve ``created_at``).
#:   - ``/v1/vectors/collections`` is a GET (collection listing), not a POST.
#: When adding a NEW migration ETL endpoint, add it here — the gate cannot
#: auto-detect an omission (the live probe only checks the routes it is given).
MIGRATION_ROUTES: tuple[str, ...] = (
    "/v1/memory/import",
    "/v1/memory/import_batch",
    "/v1/plans/import",
    "/v1/telemetry/import",
    "/v1/taxonomy/import/topic",
    "/v1/taxonomy/import/assignment",
    "/v1/taxonomy/import/link",
    "/v1/taxonomy/import/meta",
    "/v1/aspects/import",
    "/v1/aspects/highlights/import",
    "/v1/aspects/promotion/import",
    "/v1/aspects/queue/import",
    "/v1/chash/import",
    "/v1/chash/upsert_many",
    "/v1/catalog/import/owner",
    "/v1/catalog/import/document",
    "/v1/catalog/import/link",
    "/v1/catalog/import/chunk",
    "/v1/catalog/import/collection",
    "/v1/catalog/verify/relation-counts",
    "/v1/vectors/upsert-chunks",
    "/v1/vectors/collections",
)

#: HTTP statuses that mean the edge did not forward the request to the app.
_EDGE_BLOCKED_STATUSES: frozenset[int] = frozenset({403, 404})


@dataclass(frozen=True)
class RouteCoverageResult:
    """Outcome of a route-coverage probe.

    ``ok`` is True only when every route in :data:`MIGRATION_ROUTES` reached the
    application. ``unreachable`` lists the edge-blocked routes in allowlist order
    (a stable, deduplicated tuple) for an actionable go-live failure message.
    ``statuses`` is the per-route status the probe returned, for diagnostics.
    """

    ok: bool
    unreachable: tuple[str, ...]
    statuses: dict[str, int]


def check_route_coverage(
    probe: Callable[[str], int],
    routes: tuple[str, ...] = MIGRATION_ROUTES,
) -> RouteCoverageResult:
    """Probe every migration route and report which the edge blocks.

    Parameters
    ----------
    probe:
        Callable mapping a route path to the HTTP status an authenticated
        tenant-bearer request to that route returns at the edge. Inject a real
        HTTP client for the live go-live check; a stub for unit tests.
    routes:
        The routes to check (defaults to the canonical allowlist).

    Returns
    -------
    RouteCoverageResult
        ``ok=False`` with the edge-blocked routes named, or ``ok=True`` when
        every route reached the application.
    """
    statuses: dict[str, int] = {}
    unreachable: list[str] = []
    for route in routes:
        status = probe(route)
        statuses[route] = status
        if status in _EDGE_BLOCKED_STATUSES:
            unreachable.append(route)
    return RouteCoverageResult(
        ok=not unreachable,
        unreachable=tuple(unreachable),
        statuses=statuses,
    )
