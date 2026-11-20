# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client reader for ``GET /v1/catalog/uri_segment_census`` (RDR-229 P1.2, nexus-p2m1x.2).

The route groups the tenant's source-URI rows by ``(table, scheme, segment, collection)`` and returns each group's
row count with the content type its collection's registry row holds: the ``chroma://`` rows of ``catalog_documents``,
``document_aspects`` and ``document_highlights``, and their ``nx-note://`` rows. The engine does no classification;
:mod:`nexus.catalog.rdr229.uri_residue_doctor` decides which groups are residue with ``aspect_readers.FILE_ROUTED_PREFIXES``.
One answer replaces a client walk of the catalog (RDR-241). Body: ``{count, limit, capped, groups}``, each group
``{table, scheme, segment, collection, content_type, rows}``; ``count`` is every group and ``capped`` means the list
holds fewer."""
from __future__ import annotations

from typing import Any

from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable, get_route, require_flag

ROUTE: str = "/uri_segment_census"
#: The ``GET /version`` flag that says the engine answers the route (engine-service 0.1.200).
FLAG: str = "catalog_uri_segment_census_supported"
#: The route's bound on ``limit`` (``UriSegmentCensusReads.MAX_LIMIT``).
CENSUS_MAX: int = 10_000
#: Client timeout: above the engine's 10 s statement bound (whose cut answers 503), under the public edge's cut.
CENSUS_TIMEOUT_S: float = 15.0
TABLES: tuple[str, ...] = ("catalog_documents", "document_aspects", "document_highlights")
SCHEMES: tuple[str, ...] = ("chroma", "nx-note")


def _group_ok(grp: Any) -> bool:
    return (isinstance(grp, dict) and grp.get("table") in TABLES and grp.get("scheme") in SCHEMES
            and all(isinstance(grp.get(k), str) for k in ("segment", "collection", "content_type"))
            and isinstance(grp.get("rows"), int) and not isinstance(grp.get("rows"), bool) and grp["rows"] > 0)


def read_uri_segment_census(client: Any, limit: int = CENSUS_MAX) -> dict:
    """The route's body from one request. Raises :class:`EngineRouteUnavailable` when the engine does not answer (no
    request is sent without the flag) or the body is not the contract: an uncapped list holds exactly ``count`` groups,
    every group a known table and scheme with string fields and a positive row count."""
    if not 1 <= limit <= CENSUS_MAX:
        raise ValueError(f"limit must be 1..{CENSUS_MAX}, got {limit}")
    require_flag(client, ROUTE, FLAG)
    body = get_route(client, ROUTE, FLAG, timeout=CENSUS_TIMEOUT_S, limit=limit)
    if not isinstance(body, dict):
        raise EngineRouteUnavailable(ROUTE, FLAG, EngineRouteUnavailable.MALFORMED, type(body).__name__)
    count, capped, groups = body.get("count"), body.get("capped"), body.get("groups")
    if (not isinstance(count, int) or isinstance(count, bool) or count < 0 or not isinstance(capped, bool)
            or not isinstance(groups, list) or len(groups) > limit or not all(_group_ok(x) for x in groups)
            or (not capped and len(groups) != count)):
        raise EngineRouteUnavailable(ROUTE, FLAG, EngineRouteUnavailable.MALFORMED, f"keys {sorted(body)}")
    return {"count": count, "limit": body.get("limit", limit), "capped": capped, "groups": groups}
