# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-250 Phase 1 Steps 2 and 3 (nexus-io7tw): the pool key scan of the exact-key resolution branch.

The resolution branch (:meth:`~nexus.plans.coverage_ledger.CoverageBoundary._resolve_anchors`) scans the generator's
evidence, the anchors, for entity keys. A search step returns more chunks than the generator reads (plan 10: 40 hits, 20
read), so a key named only in the unread rest is never resolved. This module scans that rest, the **pool**, behind
``NX_ANSWER_POOL_KEY_SCAN`` (:func:`nexus.plans.switches.pool_key_scan_enabled`, default off).

**The pool** is the search step's ids that are not already an anchor, fetched in one call through ``view.fetch`` (an
accounted ``sql`` step, no model call) and scanned with the same ``find_key_tokens`` and ``strip_source_note`` rule as an
anchor. A chunk with no key is dropped. A key found only in the pool is a *candidate*. Which step is the search step: the
first step of the run whose tool is ``search`` and whose output carries ``ids``; a plan with none has no pool.

**The precision filter** keeps a candidate key only when the coverage assessment cites its chunk. The chunks that carry a
candidate key go through the boundary's own assessment (``CoverageBoundary._assess``: its instruction, its facet groups,
its packing by ``_pack``, its reply parsing, its budget and deadline checks), against the request's facets, and a key is
kept when its chunk's item id is in the ``evidence_ids`` of a facet the assessment marks ``covered``. Keys the anchors
name are not filtered. Without the filter a pool key is projected at precision 0.715, below the 0.95 gate (250-research-4),
so the filter is part of the design and the switch stays off until the MVV measures it.

**Failure is closed.** A fetch failure, an assessment that does not finish ``ok`` (it raised, replied unusably, or only
part of it ran), a request with no facet, a USD allowance below one assessment call's mean cost, a deadline under the
boundary's minimum call time, or a filter that outlives its slice of the repair's retrieval allowance keeps no candidate
key, and ``pool_reason`` names why. Every candidate is then listed in ``pool_keys_rejected``, so a reader finds each key as
kept or rejected, never absent. The anchors' own keys resolve as they do without the switch.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from nexus.plans.coverage_ledger import AssessmentReport, BoundaryView, EvidenceAnchor, FacetFinding
from nexus.plans.coverage_repair import MIN_REPAIR_QUERY_S, RepairHit
from nexus.plans.request_coverage import Facet
from nexus.plans.reserved_stream import strip_source_note

_log = structlog.get_logger(__name__)

__all__ = ["POOL_REASONS", "PoolScan", "scan_pool"]

#: Why the pool was skipped or closed (``RepairReport.pool_reason``); empty when the scan ran through.
POOL_REASONS: tuple[str, ...] = (
    "no_search_step",  # the plan has no search step carrying ids: no pool
    "no_fetch",  # the runner gave the boundary no fetch hook
    "fetch_failed",  # the one chunk fetch raised: nothing was scanned
    "no_facets",  # the request has no facet to judge a chunk's support by
    "filter_skipped_budget",  # the USD allowance was spent or below one assessment call's mean cost
    "filter_skipped_deadline",  # under the minimum time of one assessment call
    "filter_timeout",  # the filter outlived its slice of the repair's retrieval allowance
    "filter_failed",  # the assessment raised, or every call replied unusably
    "filter_partial_failure",  # some assessment calls failed: a partial verdict keeps nothing
)

#: The share of the time left to the resolve step that the filter may use; the rest stays for ``resolve`` and the fetch.
FILTER_TIME_SHARE: float = 0.5

_POOL_ID_PREFIX = "p"

#: ``assess(facets, anchors, view, reserve)``: :meth:`CoverageBoundary._assess`.
AssessCall = Callable[..., Awaitable[tuple[dict[str, FacetFinding], AssessmentReport]]]


@dataclass(frozen=True)
class PoolScan:
    """What the pool scan did: ``pairs`` the (pool chunk, key) pairs that join the anchors' pairs (kept keys only, each on
    the chunks the assessment cited), and ``fields`` the :class:`RepairReport` fields it sets."""

    pairs: tuple[tuple[EvidenceAnchor, str], ...]
    fields: dict[str, Any]


def _fields(*, scanned: int = 0, candidates: Sequence[str] = (), kept: Sequence[str] = (), calls: int = 0, reason: str = "") -> dict[str, Any]:
    kept_set = set(kept)
    return {
        "pool_key_scan": True, "pool_scanned": scanned, "pool_candidate_keys": tuple(candidates), "pool_keys_kept": tuple(kept),
        "pool_keys_rejected": tuple(k for k in candidates if k not in kept_set), "pool_filter_calls": calls, "pool_reason": reason,
    }


def _search_ids(view: BoundaryView) -> Mapping[str, Any] | None:
    """The first step's output whose tool is ``search`` and which carries ``ids``, or ``None``."""
    for i, out in enumerate(view.step_outputs):
        tool = view.tools[i] if i < len(view.tools) else ""
        if tool == "search" and isinstance(out, Mapping) and isinstance(out.get("ids"), list):
            return out
    return None


async def scan_pool(
    view: BoundaryView, *, anchors: Sequence[EvidenceAnchor], anchor_keys: frozenset[str], config: Any, facets: Sequence[Facet],
    hits_of: Callable[[Any], list[RepairHit]], in_scope: Callable[[RepairHit], bool], assess: AssessCall,
    call_timeout: Callable[[float | None], float], resolve_until: float, fetch_until: float,
) -> PoolScan:
    """Scan the search step's non-anchor chunks for entity keys and keep the keys the assessment cites.

    *anchors* are the first ledger's anchors and *anchor_keys* the keys they name. *hits_of* reads a step output's ids
    as :class:`RepairHit`, *in_scope* is the boundary's scope test for a hit, *assess* its assessment pass and
    *call_timeout* its per-call timeout rule. *resolve_until* and *fetch_until* are the repair's monotonic stamps: the
    fetch is bounded by the second, the filter by half of what is left to the first (:data:`FILTER_TIME_SHARE`).
    A malformed entity config raises :class:`~nexus.entity_keys.EntityConfigError` to the caller, as the anchor scan does.
    """
    from nexus.entity_keys import find_key_tokens  # noqa: PLC0415 — deferred: the ledger stays light at import

    out = _search_ids(view)
    if out is None:
        return PoolScan((), _fields(reason="no_search_step"))
    held = {a.chash.lower() for a in anchors if a.chash}
    hits = [h for h in hits_of(out) if (h.chash or h.id).lower() not in held and in_scope(h)]
    if not hits:
        return PoolScan((), _fields())
    if view.fetch is None:
        return PoolScan((), _fields(reason="no_fetch"))
    try:
        fetched = await view.fetch(hits, timeout=max(MIN_REPAIR_QUERY_S, fetch_until - time.monotonic()))
    except Exception as exc:  # noqa: BLE001 — a failed pool fetch leaves the anchors' keys to resolve as they would have
        _log.warning("nx_answer_pool_fetch_failed", error=type(exc).__name__)
        return PoolScan((), _fields(reason="fetch_failed"))
    held_texts = {strip_source_note(t) for t in view.evidence_texts if isinstance(t, str)}
    pool: list[tuple[EvidenceAnchor, tuple[str, ...]]] = []  # a chunk with a key, in fetch order
    scanned = 0
    for item in fetched:
        bare = strip_source_note(item.text) if item.text else ""
        if not bare or bare in held_texts:  # empty, or already one of the generator's inputs
            continue
        held_texts.add(bare)
        scanned += 1
        keys = tuple(dict.fromkeys(find_key_tokens(bare, item.collection, config=config)))
        if keys:
            anchor = EvidenceAnchor(
                f"{_POOL_ID_PREFIX}{len(pool) + 1}", "vector", "chunk", (item.chash or hashlib.sha256(bare.encode("utf-8")).hexdigest()).lower(),
                item.collection, item.tumbler, "", "", item.text)
            pool.append((anchor, keys))
    candidates = tuple(dict.fromkeys(k for _, keys in pool for k in keys if k not in anchor_keys))
    if not candidates:
        return PoolScan((), _fields(scanned=scanned))

    def closed(reason: str, calls: int = 0) -> PoolScan:
        return PoolScan((), _fields(scanned=scanned, candidates=candidates, calls=calls, reason=reason))

    if not facets:
        return closed("no_facets")
    usd, estimate = view.remaining_usd(), view.assessment_cost_estimate()
    if usd is not None and (usd <= 0 or (estimate is not None and usd < estimate)):
        return closed("filter_skipped_budget")
    seconds = view.remaining_seconds()
    if seconds is not None and call_timeout(seconds) <= 0:
        return closed("filter_skipped_deadline")
    carriers = [a for a, keys in pool if any(k in candidates for k in keys)]
    budget = max(MIN_REPAIR_QUERY_S, (resolve_until - time.monotonic()) * FILTER_TIME_SHARE)
    try:
        findings, report = await asyncio.wait_for(assess(list(facets), carriers, view, 0.0), timeout=budget)
    except TimeoutError:
        _log.warning("nx_answer_pool_filter_timeout", budget_s=round(budget, 1))
        return closed("filter_timeout")
    except Exception as exc:  # noqa: BLE001 — a failed filter keeps no pool key; the anchors' keys resolve as they would have
        _log.warning("nx_answer_pool_filter_failed", error=type(exc).__name__)
        return closed("filter_failed")
    if report.outcome != "ok":
        reason = {"skipped_budget": "filter_skipped_budget", "skipped_deadline": "filter_skipped_deadline",
                  "partial_failure": "filter_partial_failure"}.get(report.outcome, "filter_failed")
        return closed(reason, report.calls)
    cited = {i for f in findings.values() if f.state == "covered" for i in f.evidence_ids}
    kept_chunks = [(a, keys) for a, keys in pool if a.item_id in cited]
    kept_keys = {k for _, keys in kept_chunks for k in keys}
    kept = tuple(k for k in candidates if k in kept_keys)
    pairs = tuple((a, k) for a, keys in kept_chunks for k in keys if k in kept)
    return PoolScan(pairs, _fields(scanned=scanned, candidates=candidates, kept=kept, calls=report.calls))
