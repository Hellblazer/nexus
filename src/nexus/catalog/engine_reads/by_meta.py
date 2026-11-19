# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client reader for ``GET /v1/catalog/docs/by-meta`` (RDR-241 Phase 2, nexus-uwggq.2.7).

The route answers the tenant's live documents whose own ``meta`` satisfies every clause, one page at a time in the
engine's text order of tumbler, as ``{count, limit, offset, documents}`` with an exact ``count``. Clauses:
``meta.KEY=VALUE`` (the reading rules of :func:`nexus.catalog.meta_filter.meta_matches`), ``meta_min.KEY=N`` (the
rule of :func:`nexus.catalog.meta_filter.meta_at_least`), ``sensitive=true``, and an optional exact
``content_type``. A key whose absence has a stated meaning (:data:`nexus.catalog.meta_filter.ABSENT_MEANS`) travels
with ``meta_default.KEY``, so the default table stays in ``meta_filter.py``; the engine refuses a default sent without
its ``meta.KEY``."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from nexus.catalog.engine_reads.route_unavailable import DOC_PAGE_MAX, read_doc_page
from nexus.catalog.meta_filter import ABSENT_MEANS

ROUTE: str = "/docs/by-meta"
#: The ``GET /version`` flag that says the engine answers the route.
FLAG: str = "catalog_docs_by_meta_supported"


def read_docs_by_meta(client: Any, *, content_type: str | None = None, meta: Sequence[tuple[str, str]] = (),
                      meta_min: Sequence[tuple[str, int]] = (), sensitive: bool = False, limit: int = DOC_PAGE_MAX,
                      offset: int = 0) -> tuple[list[dict], int]:
    """One page of the live documents that satisfy every clause: ``(documents, count)``.

    At least one ``meta``, ``meta_min`` or ``sensitive`` clause is required, so the route never serves as an
    unfiltered listing. Two ``meta`` pairs on one key with different values match nothing and are answered
    ``([], 0)`` without a request; an empty key or value raises ``ValueError`` (the transport drops an empty
    parameter, which would widen the answer). Raises
    :class:`~nexus.catalog.engine_reads.route_unavailable.EngineRouteUnavailable` when the engine does not answer
    (no request is sent without the flag)."""
    wanted: dict[str, str] = {}
    for key, value in meta:
        if not key or value == "":
            raise ValueError(f"a metadata clause needs a key and a non-empty value, got {key!r}={value!r}")
        if wanted.setdefault(key, value) != value:
            return [], 0
    bounds: dict[str, int] = {}
    for key, n in meta_min:
        if not key or not isinstance(n, int) or isinstance(n, bool):
            raise ValueError(f"a metadata bound needs a key and an integer, got {key!r}={n!r}")
        bounds[key] = max(n, bounds.get(key, n))
    if not wanted and not bounds and not sensitive:
        raise ValueError("by-meta needs at least one meta, meta_min or sensitive clause")
    params: dict[str, Any] = {f"meta.{k}": v for k, v in wanted.items()}
    params |= {f"meta_default.{k}": ABSENT_MEANS[k] for k in wanted if k in ABSENT_MEANS}
    params |= {f"meta_min.{k}": str(n) for k, n in bounds.items()}
    if sensitive:
        params["sensitive"] = "true"
    return read_doc_page(client, ROUTE, FLAG, limit=limit, offset=offset, content_type=content_type or None, **params)
