# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client reader for ``GET /v1/catalog/sensitive/locators`` (RDR-241 Phase 2, nexus-uwggq.2.13).

The route answers the tenant's live sensitive documents and the live documents whose ``alias_of`` names a live
sensitive one, in tumbler order, as ``{count, limit, capped, documents}`` (the envelope of ``/sensitive``); each row
is ``{tumbler, source_uri, file_path, alias_of, physical_collection, sensitive}``, ``sensitive`` being the row's own
flag (an alias row reads false unless it is itself flagged). Operator dispatch writes its file-tool deny rules from
these rows instead of listing the catalog (``operators/sensitive_paths.py``)."""
from __future__ import annotations

from typing import Any

from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable, get_route, require_flag

ROUTE: str = "/sensitive/locators"
#: The ``GET /version`` flag that says the engine answers the route.
FLAG: str = "catalog_sensitive_locators_supported"
#: The route's bound on ``limit`` (``CatalogSensitiveLocatorReads.MAX_LIMIT``).
LOCATORS_MAX: int = 10_000
#: Client timeout: above the engine's 10 s statement bound, under the public edge's roughly 30 s cut.
LOCATORS_TIMEOUT_S: float = 15.0
_TEXT_FIELDS = ("source_uri", "file_path", "alias_of", "physical_collection")


def read_sensitive_locators(client: Any, limit: int = LOCATORS_MAX) -> dict:
    """``{"count", "limit", "capped", "documents"}`` from one request. ``capped`` is the engine's answer and is left
    for the caller to act on (operator dispatch reads a capped list as "cannot say"). Raises
    :class:`EngineRouteUnavailable` when the engine does not answer (no request is sent without the flag), or when
    the body is not the contract: a row needs a non-empty string ``tumbler``, a boolean ``sensitive`` and strings
    or nulls for the other four fields, and an uncapped list holds exactly ``count`` rows."""
    if not 1 <= limit <= LOCATORS_MAX:
        raise ValueError(f"limit must be 1..{LOCATORS_MAX}, got {limit}")
    require_flag(client, ROUTE, FLAG)
    body = get_route(client, ROUTE, FLAG, timeout=LOCATORS_TIMEOUT_S, limit=limit)
    if not isinstance(body, dict):
        raise EngineRouteUnavailable(ROUTE, FLAG, EngineRouteUnavailable.MALFORMED, type(body).__name__)
    count, capped, docs = body.get("count"), body.get("capped"), body.get("documents")

    def row_ok(d: Any) -> bool:
        return (isinstance(d, dict) and isinstance(d.get("tumbler"), str) and bool(d["tumbler"])
                and isinstance(d.get("sensitive"), bool)
                and all(d.get(f) is None or isinstance(d.get(f), str) for f in _TEXT_FIELDS))

    if (not isinstance(count, int) or isinstance(count, bool) or count < 0 or not isinstance(capped, bool)
            or not isinstance(docs, list) or len(docs) > limit or not all(row_ok(d) for d in docs)
            or (not capped and len(docs) != count)):
        raise EngineRouteUnavailable(ROUTE, FLAG, EngineRouteUnavailable.MALFORMED, f"keys {sorted(body)}")
    return {"count": count, "limit": body.get("limit", limit), "capped": capped, "documents": docs}
