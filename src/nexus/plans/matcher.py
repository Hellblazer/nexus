# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plan matcher — RDR-078 P1 (SC-1, SC-11, SC-12).

Two paths, same output shape:

  * **T1 cosine path** — a :class:`PlanCache` queries the
    ``plans__session`` ChromaDB collection, returns ``(plan_id,
    distance)`` pairs. We convert distance → cosine similarity
    (``1 - distance``), look up the row in :class:`~nexus.db.t2.
    plan_library.PlanLibrary`, and return :class:`~nexus.plans.match.
    Match` objects with ``confidence`` set.
  * **FTS5 fallback** — when no cache is provided or the cache has no
    hits, fall back to ``PlanLibrary.search_plans`` (keyword match
    over the stored descriptions). Matches carry ``confidence=None``
    as the sentinel.

Post-filter: the caller may pin a ``dimensions`` dict. Plans whose
``dimensions ⊇ filter.dimensions`` pass; others drop. ``min_confidence``
rejects below-threshold cosine hits, but FTS5 hits
(``confidence=None``) pass through — the sentinel is an implicit
"skill-level gate decides".

Side effect: every returned plan increments
``plans.match_count`` (and ``match_conf_sum`` when scored) via
:meth:`PlanLibrary.increment_match_metrics`. SC-12.
"""
from __future__ import annotations

from typing import Any, Protocol

from nexus.db.t2.plan_library import PlanLibrary
from nexus.plans.match import Match

__all__ = ["PlanCache", "plan_match"]


class PlanCache(Protocol):
    """Minimal interface for the T1 ``plans__session`` cosine cache.

    Implemented by :mod:`nexus.plans.session_cache` (Phase 2 of this
    bead) and stubbed in tests. Kept narrow so the matcher can be
    exercised without a real ChromaDB HTTP server.
    """

    @property
    def is_available(self) -> bool: ...

    def query(self, intent: str, n: int) -> list[tuple[int, float]]:
        """Return ``(plan_id, distance)`` pairs ordered closest-first."""
        ...


def _superset(plan_dims: dict[str, Any], filter_dims: dict[str, Any]) -> bool:
    """Return True when ``plan_dims ⊇ filter_dims`` by equality."""
    return all(plan_dims.get(k) == v for k, v in filter_dims.items())


def plan_match(
    intent: str,
    *,
    library: PlanLibrary,
    cache: PlanCache | None = None,
    dimensions: dict[str, Any] | None = None,
    scope_preference: str = "",
    context: dict[str, Any] | None = None,
    min_confidence: float = 0.85,
    n: int = 5,
    project: str = "",
) -> list[Match]:
    """Return plans ranked for *intent*.

    See module docstring for the two-path contract. ``scope_preference``
    and ``context`` are accepted for forward compatibility with Phase 2
    scoping + specificity ranking (PQ-14 / PQ-20) — unused at this
    version. Every returned plan has its ``match_count`` bumped and, when
    the confidence is numeric, ``match_conf_sum`` accumulates.

    Always returns matches sorted by confidence descending (cosine
    higher-is-better); FTS5-fallback matches preserve the rank order
    returned by ``PlanLibrary.search_plans``.

    **Sentinel**: an FTS5-fallback match sets ``Match.confidence = None``.
    The ``plan_match`` MCP tool renders this as ``confidence=fts5`` in
    its string output. Callers MUST treat ``confidence=None`` (or the
    rendered ``fts5`` string) as a match that clears the gate — the
    ``min_confidence`` parameter does not apply to FTS5 hits.
    """
    filter_dims = dimensions or {}

    # T1 cosine path when cache available + has hits.
    matches: list[Match] = []
    # Over-fetch covers both (a) dimension post-filter attrition and (b)
    # min_confidence threshold attrition. A fixed floor (n * 2) avoids the
    # under-delivery case when filter_dims is empty — cost is bounded by the
    # cache size, which is small (session-scoped plan descriptions).
    _over = max(n * 2, n + len(filter_dims) * 2)
    if cache is not None and cache.is_available:
        hits = cache.query(intent, _over)
        for plan_id, distance in hits:
            row = library.get_plan(plan_id)
            if row is None:
                continue
            confidence = max(0.0, 1.0 - float(distance))
            if confidence < min_confidence:
                continue
            m = Match.from_plan_row(row, confidence=confidence)
            if filter_dims and not _superset(m.dimensions, filter_dims):
                continue
            matches.append(m)
            if len(matches) >= n:
                break

        if matches:
            for m in matches:
                library.increment_match_metrics(m.plan_id, confidence=m.confidence)
            matches.sort(key=lambda x: x.confidence or 0.0, reverse=True)
            return matches

    # FTS5 fallback: either cache unavailable or T1 returned no hits.
    # Over-fetch so the dimension post-filter doesn't starve the caller.
    _fts_over = max(n * 2, n + len(filter_dims) * 3)
    rows = library.search_plans(intent, limit=_fts_over, project=project)
    for row in rows:
        m = Match.from_plan_row(row, confidence=None)
        if filter_dims and not _superset(m.dimensions, filter_dims):
            continue
        matches.append(m)
        if len(matches) >= n:
            break

    for m in matches:
        library.increment_match_metrics(m.plan_id, confidence=None)
    return matches
