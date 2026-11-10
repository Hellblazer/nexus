# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tell an engine that is away from one that refused or is slow (nexus-f3yqd.5).

The ladder runner waits out the first and reports the rest. A rung's own catch-all (verify reads
"unknown is not reached") lets the first through, so the runner can wait for the engine instead of
reporting a confirmation failure.
"""

from __future__ import annotations

#: HTTP statuses that mean the engine, or the proxy in front of it, is not serving yet.
_AWAY_STATUSES: frozenset[int] = frozenset({502, 503, 504})


def _transport_away(link: BaseException) -> bool:
    """A refused, reset or dropped connection, or a network error: the engine is not there. A timeout is
    not counted: an engine that answers slowly is still there, and waiting for it to restart would only
    repeat the slow read."""
    import urllib.error  # noqa: PLC0415 — stdlib

    import httpx  # noqa: PLC0415 — deferred

    if isinstance(link, (TimeoutError, httpx.TimeoutException)):
        return False
    if isinstance(link, (ConnectionError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(link, urllib.error.URLError) and not isinstance(link, urllib.error.HTTPError):
        return not isinstance(getattr(link, "reason", None), TimeoutError)
    return False


def engine_unreachable(exc: BaseException) -> bool:
    """True when *exc*, or an exception it was raised from, says the engine is away rather than that it
    refused or is slow: an endpoint with no lease, a refused or dropped connection, or a 502/503/504. An
    HTTP refusal (422, 409, ...), a malformed answer and a timeout are not outages. The chain is followed
    through ``__cause__``, and through ``__context__`` unless it was suppressed (``raise ... from None``)."""
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred, heavy module
    from nexus.db.service_endpoint import ServiceEndpointUnresolvableError  # noqa: PLC0415 — deferred

    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        if isinstance(link, ServiceEndpointUnresolvableError) or _transport_away(link):
            return True
        if isinstance(link, VectorServiceError) and getattr(link, "code", None) in _AWAY_STATUSES:
            return True
        link = link.__cause__ if link.__cause__ is not None else (None if link.__suppress_context__ else link.__context__)
    return False
