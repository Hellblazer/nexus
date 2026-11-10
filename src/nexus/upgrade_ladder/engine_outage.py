# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tell an engine that is away from one that refused (nexus-f3yqd.5).

The ladder runner waits out the first and reports the second. A rung's own catch-all (verify reads
"unknown is not reached") lets the first through, so the runner can wait for the engine instead of
reporting a confirmation failure.
"""

from __future__ import annotations


def engine_unreachable(exc: BaseException) -> bool:
    """True when *exc*, or an exception it was raised from, says the engine is away rather than that it
    refused: an endpoint with no lease, a refused or dropped connection, a timeout, or a vector-client
    transport failure (``code`` ``None``) or 503. An HTTP refusal (422, 409, ...) is not an outage."""
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — deferred, heavy module
    from nexus.db.service_endpoint import ServiceEndpointUnresolvableError  # noqa: PLC0415 — deferred
    from nexus.retry import _is_connectivity_error  # noqa: PLC0415 — the shared transport classifier

    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        if isinstance(link, ServiceEndpointUnresolvableError) or _is_connectivity_error(link):
            return True
        if isinstance(link, VectorServiceError) and getattr(link, "code", None) in (None, 503):
            return True
        link = link.__cause__ or link.__context__
    return False
