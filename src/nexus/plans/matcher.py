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

import structlog

from nexus.db.t2.plan_library import PlanLibrary
from nexus.plans.match import Match
from nexus.plans.scope import _normalize_scope_string

__all__ = ["PlanCache", "plan_match"]

_log = structlog.get_logger()


# ── Scope-aware re-ranking (RDR-091 Phase 2b + 2c) ─────────────────────────
#
# Score formula (RDR-091 §Proposed Solution → Score formula):
#     final_score = base_confidence * (1 + _SCOPE_FIT_WEIGHT * scope_fit)
# scope_fit is 0.0 (agnostic plan) or 1.0 (scope prefix match).
#
# 0.15 picked by inspection against 5 fixture pairs in test_plan_match.py
# (Phase 2c, nexus-svcg). The motivating failure case (RDR §Problem
# Statement, generic agnostic at 0.82 vs specialized matching at 0.79)
# breaks even at weight ≈ 0.038; 0.15 leaves comfortable margin
# (specialized 0.79 * 1.15 = 0.9085 beats generic 0.82) without so
# much boost that a genuinely higher-cosine agnostic plan gets drowned.
_SCOPE_FIT_WEIGHT: float = 0.15


def _scope_fit(plan_scope_tags: str, normalized_scope_pref: str) -> float | None:
    """Return scope-fit in ``{0.0, 1.0}`` or ``None`` for a conflict.

    Semantics (RDR-091 §Proposed Solution → scope-fit):
      * empty caller scope → always ``0.0`` (no preference; no filter)
      * empty plan scope_tags (agnostic plan) → ``0.0`` (neutral; stays)
      * any tag in *plan_scope_tags* prefix-matches the caller scope in
        either direction (``tag.startswith(scope) or scope.startswith(tag)``)
        → ``1.0`` (bare-family plans serve narrower queries and vice versa)
      * otherwise → ``None`` (conflict; caller filters out)
    """
    if not normalized_scope_pref:
        return 0.0
    if not plan_scope_tags:
        return 0.0
    tags = [t for t in plan_scope_tags.split(",") if t]
    for tag in tags:
        if tag.startswith(normalized_scope_pref) or normalized_scope_pref.startswith(tag):
            return 1.0
    return None


def _specificity(plan_scope_tags: str) -> int:
    """Tie-break key: fewer scope_tags means a more specific plan.

    Returned as a negative count so higher values sort first under
    Python's ``reverse=True`` sort.
    """
    return -len([t for t in plan_scope_tags.split(",") if t])


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
    # RDR-079 P5 calibration (docs/rdr/rdr-079-calibration.md) picked
    # 0.40 as the F1-optimal threshold for the bundled MiniLM T1 cache.
    # Callers that need precision-first behavior override explicitly
    # (0.50 → precision 0.90 at the cost of recall 0.19).
    min_confidence: float = 0.40,
    n: int = 5,
    project: str = "",
) -> list[Match]:
    """Return plans ranked for *intent*.

    See module docstring for the two-path contract. ``scope_preference``
    is the RDR-091 Phase 2b filter + re-ranker (was a no-op prior):

      * A non-empty *scope_preference* is normalized (hash-suffix and
        glob stripped) before comparison against each plan's
        ``scope_tags``.
      * **Scope-conflict filter**: a plan whose ``scope_tags`` is
        non-empty and none of whose tags prefix-match the caller scope
        (in either direction) is dropped from the candidate pool. Agnostic
        plans (``scope_tags == ''``) are kept with neutral weight.
      * **Scope-fit boost**: matching plans receive a small
        multiplicative boost per the RDR-091 score formula
        ``adjusted = confidence * (1 + _SCOPE_FIT_WEIGHT * scope_fit)``,
        used only for ranking. ``Match.confidence`` still carries the
        raw cosine, so ``min_confidence`` and downstream thresholds
        are unchanged.
      * **Specificity tie-break**: at equal adjusted score, the plan
        with fewer scope_tags (more specific scope) ranks first.

    ``context`` is still accepted for forward compatibility and currently
    unused. Every returned plan has its ``match_count`` bumped and, when
    the confidence is numeric, ``match_conf_sum`` accumulates.

    Always returns matches sorted descending by the ranking key
    (scope-adjusted for T1, FTS5 order preserved for the fallback).
    Zero-candidate outcome (e.g. every candidate conflicts) returns
    ``[]``; upstream ``nx_answer`` falls through to the inline planner.

    **Sentinel**: an FTS5-fallback match sets ``Match.confidence = None``.
    The ``plan_match`` MCP tool renders this as ``confidence=fts5`` in
    its string output. Callers MUST treat ``confidence=None`` (or the
    rendered ``fts5`` string) as a match that clears the gate — the
    ``min_confidence`` parameter does not apply to FTS5 hits.
    """
    filter_dims = dimensions or {}
    scope_pref = _normalize_scope_string(scope_preference) if scope_preference else ""

    # T1 cosine path when cache available + has hits.
    # Over-fetch covers (a) dimension post-filter attrition, (b) min_confidence
    # threshold attrition, and (c) RDR-091 scope-conflict attrition. A fixed
    # floor (n * 2) avoids under-delivery when filter_dims is empty. Cost is
    # bounded by cache size (session-scoped).
    _over = max(n * 2, n + len(filter_dims) * 2)
    if scope_pref:
        _over += n  # extra budget for potential conflict drops
    if cache is not None and cache.is_available:
        hits = cache.query(intent, _over)
        # Gather all admissible candidates with (adjusted_score, specificity).
        scored: list[tuple[float, int, Match]] = []
        scope_conflict_drops = 0
        for plan_id, distance in hits:
            row = library.get_plan(plan_id)
            if row is None:
                # Search review I-4: the plan was deleted from T2 but the
                # T1 cache still carries its embedding. Evict it now so
                # the stale row stops skewing future top-N fetches.
                try:
                    cache.remove(plan_id)
                except Exception:
                    pass
                continue
            confidence = max(0.0, 1.0 - float(distance))
            if confidence < min_confidence:
                continue
            m = Match.from_plan_row(row, confidence=confidence)
            if filter_dims and not _superset(m.dimensions, filter_dims):
                continue
            fit = _scope_fit(m.scope_tags, scope_pref)
            if fit is None:
                scope_conflict_drops += 1
                continue  # scope conflict — drop from pool
            adjusted = confidence * (1.0 + _SCOPE_FIT_WEIGHT * fit)
            scored.append((adjusted, _specificity(m.scope_tags), m))

        if scored:
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            matches = [m for _, _, m in scored[:n]]
            for m in matches:
                library.increment_match_metrics(m.plan_id, confidence=m.confidence)
            return matches

        # T1 returned hits but every admissible plan was scope-filtered.
        # Log so operators can see when scope_preference is silently
        # degrading matches to FTS5 / inline planner (RDR-091 code-review
        # finding I-4).
        if hits and scope_conflict_drops > 0:
            _log.debug(
                "plan_match_scope_conflict_fallthrough",
                t1_hits=len(hits),
                dropped=scope_conflict_drops,
                scope_pref=scope_pref,
            )

    # FTS5 fallback: either cache unavailable or T1 returned no hits.
    # Over-fetch so the dimension post-filter doesn't starve the caller.
    _fts_over = max(n * 2, n + len(filter_dims) * 3)
    if scope_pref:
        _fts_over += n
    rows = library.search_plans(intent, limit=_fts_over, project=project)
    matches: list[Match] = []
    for row in rows:
        m = Match.from_plan_row(row, confidence=None)
        if filter_dims and not _superset(m.dimensions, filter_dims):
            continue
        if _scope_fit(m.scope_tags, scope_pref) is None:
            continue  # scope conflict on the fallback path too
        matches.append(m)
        if len(matches) >= n:
            break

    for m in matches:
        library.increment_match_metrics(m.plan_id, confidence=None)
    return matches
