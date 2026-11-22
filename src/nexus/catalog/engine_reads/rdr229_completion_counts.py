# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client reader for ``GET /v1/catalog/rdr229_completion_counts`` (RDR-229 P3.1, nexus-p2m1x.17, review N2).

``GET /v1/status`` is unauthenticated, so its ``rdr229_completion`` field carries no number that describes tenants. This route
is authenticated and answers for the CALLER'S tenant only: its registry rows and live collections, the registry rows that hold
the superseded_by CHECK back, its dangling tombstones and self-id rows by content type (R7's pre-flight), and, for each table
whose CHECK is NOT VALID, the tenant's orphan and unstamped rows. It is never another tenant's count and never a total. Body::

    {registry_rows, live_collections, superseded_by_waiting,
     dangling_tombstones: {total, by_content_type: {type: n}, self_id: {total, by_content_type: {type: n}}},
     tables: [{table, orphans, unstamped}]}

``nx doctor`` (:mod:`nexus.rdr229_completion_health`) reads it for the numbers its rows show. An engine without the route
answers 404 and the row says the counts were not read; they are never read as zero."""
from __future__ import annotations

from typing import Any

from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable, get_route, require_flag

ROUTE: str = "/rdr229_completion_counts"
#: The ``GET /version`` flag that says the engine answers the route.
FLAG: str = "catalog_rdr229_completion_counts_supported"
#: Client timeout: above the engine's 10 s statement bound (whose cut answers 503), under the public edge's cut.
TIMEOUT_S: float = 15.0


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _by_type(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and _count(v) for k, v in value.items())


def _dangling_ok(d: Any) -> bool:
    if not isinstance(d, dict) or not _count(d.get("total")) or not _by_type(d.get("by_content_type")):
        return False
    own = d.get("self_id")
    return isinstance(own, dict) and _count(own.get("total")) and _by_type(own.get("by_content_type"))


def _table_ok(t: Any) -> bool:
    return (isinstance(t, dict) and isinstance(t.get("table"), str) and bool(t["table"])
            and _count(t.get("orphans")) and _count(t.get("unstamped")))


def read_rdr229_completion_counts(client: Any) -> dict:
    """The route's body from one request. Raises :class:`EngineRouteUnavailable` when the engine does not answer (no request is
    sent without the flag) or the body is not the contract."""
    require_flag(client, ROUTE, FLAG)
    body = get_route(client, ROUTE, FLAG, timeout=TIMEOUT_S)
    if not isinstance(body, dict):
        raise EngineRouteUnavailable(ROUTE, FLAG, EngineRouteUnavailable.MALFORMED, type(body).__name__)
    tables = body.get("tables")
    if (not all(_count(body.get(k)) for k in ("registry_rows", "live_collections", "superseded_by_waiting"))
            or not _dangling_ok(body.get("dangling_tombstones")) or not isinstance(tables, list)
            or not all(_table_ok(t) for t in tables)):
        raise EngineRouteUnavailable(ROUTE, FLAG, EngineRouteUnavailable.MALFORMED, f"keys {sorted(body)}")
    return body
