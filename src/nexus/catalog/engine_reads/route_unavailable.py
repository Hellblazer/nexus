# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one error the RDR-241 Phase 2 readers raise when the engine gives no answer, and the shared page reader of the
paged document routes (``/docs/by-author``, ``/docs/by-meta``).

An absent route is never an empty answer and never a reason to walk the catalog (RDR-241): the caller reports the
cause. :attr:`EngineRouteUnavailable.reason` tells an engine without the route (a message about the engine) from a
timeout or a failed request (a retry)."""
from __future__ import annotations

from typing import Any

import httpx

#: The first engine-service release that carries the RDR-241 Phase 2 routes. The engine handoff (nexus-uwggq.2.11)
#: confirms the number and the landing (nexus-uwggq.2.18) raises ``REQUIRED_ENGINE_VERSION`` to it.
PHASE2_ROUTES_ENGINE: str = "0.1.197"


class EngineRouteUnavailable(Exception):
    """The engine did not answer an RDR-241 route; :attr:`reason` says why and :attr:`route` names it."""

    #: The engine answered ``GET /version`` without the route's flag, or the route answers 404.
    UNSUPPORTED = "unsupported"
    #: ``GET /version`` itself failed, so the engine said nothing about the route.
    PROBE_FAILED = "probe_failed"
    #: The route answered 503: the engine's statement bound, or the service unavailable.
    TIMEOUT = "timeout"
    #: Any other status, or a transport error.
    FAILED = "failed"
    #: The body is not the route's contract.
    MALFORMED = "malformed"

    def __init__(self, route: str, flag: str, reason: str, detail: str = "") -> None:
        self.route, self.flag, self.reason = route, flag, reason
        super().__init__(self.describe() + (f" ({detail})" if detail else ""))

    def describe(self) -> str:
        """One sentence a user can act on, naming the route and, for an engine without it, the engine that has it."""
        what = f"GET /v1/catalog{self.route}"
        if self.reason == self.UNSUPPORTED:
            return (f"the engine does not answer {what}; engine-service {PHASE2_ROUTES_ENGINE} or later does "
                    f"(it advertises {self.flag} on GET /version)")
        if self.reason == self.PROBE_FAILED:
            return f"GET /version failed, so whether the engine answers {what} is unknown"
        if self.reason == self.TIMEOUT:
            return f"the engine could not answer {what} in time"
        if self.reason == self.MALFORMED:
            return f"the engine's answer to {what} is not the route's contract"
        return f"the engine's answer to {what} failed"


def require_flag(client: Any, route: str, flag: str) -> None:
    """Raise :class:`EngineRouteUnavailable` unless *client*'s cached ``GET /version`` holds *flag* as a JSON ``true``.
    Nothing is sent to the route then."""
    if client._version_flags().get(flag) is True:
        return
    failed = bool(getattr(client, "_version_probe_failed", False))
    raise EngineRouteUnavailable(route, flag, EngineRouteUnavailable.PROBE_FAILED if failed
                                 else EngineRouteUnavailable.UNSUPPORTED)


def get_route(client: Any, route: str, flag: str, *, timeout: float, **params: Any) -> Any:
    """One request to *route*, sent once (a retry of a request the engine cut at its bound would run the statement
    again). A 404 is an engine without the route, a 503 its timeout; any other failure is ``FAILED``."""
    try:
        return client._get(route, idempotent=False, timeout=timeout, **params)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        reason = {404: EngineRouteUnavailable.UNSUPPORTED, 503: EngineRouteUnavailable.TIMEOUT}.get(
            status, EngineRouteUnavailable.FAILED)
        raise EngineRouteUnavailable(route, flag, reason, f"HTTP {status}") from exc
    except EngineRouteUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — any transport failure is "cannot say", reported by the caller
        raise EngineRouteUnavailable(route, flag, EngineRouteUnavailable.FAILED, type(exc).__name__) from exc


#: The paged routes' bound on ``limit`` (``CatalogDocReads.MAX_LIMIT``).
DOC_PAGE_MAX: int = 1000
#: One page's client timeout: above the engine's 10 s statement bound, whose cut answers 503, and under the public
#: edge's roughly 30 s cut (as ``by_scheme``).
DOC_PAGE_TIMEOUT_S: float = 15.0


def read_doc_page(client: Any, route: str, flag: str, *, limit: int, offset: int,
                  **params: Any) -> tuple[list[dict], int]:
    """One page of a paged document route: ``(documents, count)``, ``count`` being the exact number of matches
    whatever the page. The flag is checked first; the body must be ``{count, limit, offset, documents}`` with
    ``offset`` echoed, at most ``limit`` documents, each a map with a non-empty string ``tumbler``, else
    ``MALFORMED``."""
    if not 1 <= limit <= DOC_PAGE_MAX or offset < 0:
        raise ValueError(f"limit must be 1..{DOC_PAGE_MAX} and offset at least 0, got {limit}, {offset}")
    require_flag(client, route, flag)
    body = get_route(client, route, flag, timeout=DOC_PAGE_TIMEOUT_S, limit=limit, offset=offset, **params)
    if not isinstance(body, dict):
        raise EngineRouteUnavailable(route, flag, EngineRouteUnavailable.MALFORMED, type(body).__name__)
    count, docs = body.get("count"), body.get("documents")
    if (not isinstance(count, int) or isinstance(count, bool) or count < 0 or body.get("offset") != offset
            or not isinstance(docs, list) or len(docs) > limit
            or not all(isinstance(d, dict) and isinstance(d.get("tumbler"), str) and d["tumbler"] for d in docs)):
        raise EngineRouteUnavailable(route, flag, EngineRouteUnavailable.MALFORMED, f"keys {sorted(body)}")
    return docs, count
