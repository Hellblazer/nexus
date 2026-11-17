# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-250 Phase 1 Steps 2 and 3 (nexus-io7tw): the pool key scan of the exact-key resolution branch.

The resolution branch (:meth:`~nexus.plans.coverage_ledger.CoverageBoundary._resolve_anchors`) scans the generator's
evidence, the anchors, for entity keys. A search step returns more chunks than the generator reads (plan 10: 40 hits, 20
read), so a key named only in the unread rest is never resolved. This module scans that rest, the **pool**, behind
``NX_ANSWER_POOL_KEY_SCAN`` (:func:`nexus.plans.switches.pool_key_scan_enabled`, default off).

**Two phases, in this order.** Phase A resolves and joins the anchors' keys exactly as the branch does with the switch off,
before any pool work, so the switch-on floor is the switch-off result and the pool can never starve it. Phase B
(:func:`run_pool_phase`) is the pool's own: fetch, scan, filter, and a second resolve and fetch for the kept keys, inside
what the repair has left after phase A.

**The pool** is the search step's ids that are not already an anchor, fetched in one call through ``view.fetch`` (an
accounted ``sql`` step, no model call) and scanned with the same ``find_key_tokens`` and ``strip_source_note`` rule as an
anchor. A chunk with no key is dropped. A key found only in the pool is a *candidate*. Which step is the search step: the
first step of the run whose tool is ``search`` and whose output carries ``ids``; a plan with none has no pool.

**The precision filter** keeps a candidate key only when the coverage assessment cites its chunk as support. The chunks that
carry a candidate key go through the boundary's own assessment (``CoverageBoundary._assess``: its instruction, its facet
groups, its packing by ``_pack``, its reply parsing, its budget and deadline checks), against the request's facets, and a
key is kept when a ``supports`` record cites its chunk's item id (a covered facet's ``evidence_ids`` also holds its
contradicting ids, so those are not read). Keys the anchors name are not filtered. Without the filter a pool key is projected
at precision 0.715, below the 0.95 gate (250-research-4), so the filter is part of the design. The MVV passed (250-research-8); the switch stays off by
default until a separate decision turns it on. A pack the assessor answers with no evidence record is a reject verdict, not a failure.

**Bounds.** Phase B starts only when one assessment call fits: the time left to the repair's retrieval stamp (less what the
resolve and the record fetch need), and the answer's own remaining time, must cover the longest of the boundary's minimum
call time and the first assessment's recorded latency per wave of calls; and the USD left, after the reassessment's reserve
(planned calls times the mean call cost), must cover one call at its mean cost. Short of either it is skipped
(``filter_skipped_deadline``, ``filter_skipped_budget``) and nothing starts. The filter's calls see the same bounds through a
wrapped view, so each is cut at its slice, and a call that started is counted whether it finished, raised or was cut.

**Failure is closed, and partial.** A fetch failure, a request with no facet, a filter that raised or timed out, or a
phase-B resolve that failed keeps no pool key, and ``pool_reason`` names why; the anchors' keys stand as phase A left them. A
filter some of whose calls failed keeps the keys that the completed calls cited, rejects the rest and says
``filter_partial_failure``. Every candidate not kept is in ``pool_keys_rejected``, so a reader finds each as kept or rejected.
An unexpected exception in the phase is caught (``pool_error``) and phase A's result is returned.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import structlog

from nexus.plans.coverage_ledger import _TAG, AssessmentReport, BoundaryView, CoverageResult, EvidenceAnchor, FacetFinding
from nexus.plans.coverage_repair import MIN_REPAIR_QUERY_S, RepairHit
from nexus.plans.request_coverage import Facet
from nexus.plans.reserved_stream import strip_source_note

_log = structlog.get_logger(__name__)

__all__ = ["POOL_REASONS", "PoolEnv", "run_pool_phase"]

#: Why the pool was skipped or closed (``RepairReport.pool_reason``); empty when the scan ran through.
POOL_REASONS: tuple[str, ...] = (
    "cap_full",  # phase A's keys fill the cap: no pool key could be resolved, so nothing is fetched or asked
    "no_search_step",  # the plan has no search step carrying ids: no pool
    "no_fetch",  # the runner gave the boundary no fetch hook
    "fetch_failed",  # the one chunk fetch raised: nothing was scanned
    "no_facets",  # the request has no facet to judge a chunk's support by
    "resolution_failed",  # phase A's resolve or fetch failed: the pool is not tried on a backend that just failed
    "filter_skipped_budget",  # the USD left, after the reassessment's reserve, cannot carry one assessment call
    "filter_skipped_deadline",  # the time left cannot carry one assessment call
    "filter_timeout",  # a filter call outlived its slice (or the whole filter did): nothing completed
    "filter_failed",  # no filter call completed: it raised, or every reply was unusable
    "filter_empty_reply",  # no filter call completed, and every one of them replied with no evidence record: not a reject
    "filter_partial_failure",  # some filter calls failed or were skipped; the completed calls' verdicts stand
    "filter_unread",  # the pack size cap left some candidate chunks unread: their keys are rejected
    "pool_resolve_failed",  # the second resolve or its record fetch failed: kept keys joined nothing
    "pool_error",  # an unexpected exception in the phase: phase A's result stands
)

#: The share of the time left that the one pool chunk fetch may take; the filter and the second resolve keep the rest.
FETCH_TIME_SHARE: float = 0.3
#: Seconds kept back at the end of the retrieval allowance for the second resolve and its record fetch.
_TAIL_S: float = 3 * MIN_REPAIR_QUERY_S

_POOL_ID_PREFIX = "p"

#: ``assess(facets, anchors, view, reserve, support_ids)``: :meth:`CoverageBoundary._assess`.
AssessCall = Callable[..., Awaitable[tuple[dict[str, FacetFinding], AssessmentReport]]]
#: ``join(view, pairs, scan, cap=, resolve_until=, fetch_until=)``: :meth:`CoverageBoundary._join_keys`.
JoinCall = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class PoolEnv:
    """What phase B takes from the boundary: its entity config and request facets, the key cap, the scope test and hit
    reader, its assessment and join calls, its call-time rules, the repair's two monotonic stamps and the USD held back
    for the reassessment (``None``: the cost is unknown)."""

    config: Any
    facets: Sequence[Facet]
    cap: int
    hits_of: Callable[[Any], list[RepairHit]]
    in_scope: Callable[[RepairHit], bool]
    assess: AssessCall
    join: JoinCall
    min_call_s: float
    max_concurrent_calls: int
    resolve_until: float
    fetch_until: float
    usd_reserve: float | None = None


#: The ``supports`` records one call's log entry lists; the rest are counted in ``supports_truncated``.
_CALL_SUPPORTS_CAP = 50


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _call_record(ordinal: int, args: Mapping[str, Any], reply: Any, outcome: str) -> dict[str, Any]:
    """What one filter call sent and got back, as closed names, hashes and ids: no chunk text, no quote.

    *outcome* is ``supports`` (a record cites an item as support), ``no_support`` (records, none of them ``supports``: a
    genuine reject), ``empty`` (an evidence list with no record), ``malformed`` (not the check shape), ``raised`` or
    ``timeout``. The payload hash covers the items and the instruction, which is all the boundary sends; the check's own
    prompt wrapper is a constant of the operator and is not hashed."""
    payload = f"{args.get('items', '')}\n{args.get('check_instruction', '')}"
    record: dict[str, Any] = {
        "call": ordinal, "prompt_sha256": _digest(payload), "reply_chars": None, "reply_sha256": None, "output_tokens": None,
        "supports": [], "supports_truncated": 0, "outcome": outcome,
    }
    if outcome in ("raised", "timeout"):
        return record
    raw = json.dumps(reply, sort_keys=True, default=str, ensure_ascii=False)
    record["reply_chars"], record["reply_sha256"] = len(raw), _digest(raw)
    usage = reply.get("usage") if isinstance(reply, Mapping) else None
    tokens = usage.get("output_tokens") if isinstance(usage, Mapping) else None
    record["output_tokens"] = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None
    cited: list[dict[str, str]] = []
    evidence = reply.get("evidence") if isinstance(reply, Mapping) else None
    for rec in evidence if isinstance(evidence, list) else ():
        if not isinstance(rec, Mapping) or rec.get("role") != "supports":
            continue
        quote = rec.get("quote")
        tag = _TAG.match(quote) if isinstance(quote, str) else None
        cited.append({"item_id": str(rec.get("item_id")), "facet_id": tag.group(1) if tag else ""})
    record["supports"] = cited[:_CALL_SUPPORTS_CAP]
    record["supports_truncated"] = max(0, len(cited) - _CALL_SUPPORTS_CAP)
    return record


def _outcome(reply: Any) -> str:
    evidence = reply.get("evidence") if isinstance(reply, Mapping) else None
    if not isinstance(evidence, list):
        return "malformed"
    records = [r for r in evidence if isinstance(r, Mapping)]
    if not records:
        return "empty"
    return "supports" if any(r.get("role") == "supports" for r in records) else "no_support"


class _Tally:
    """The filter's own count of its calls: a call that started is counted whether it replied, raised or was cut."""

    def __init__(self) -> None:
        self.calls = 0
        self.empty = 0  # calls answered with no evidence record: a failed assessment, never a reject (nexus-w8d30)
        self.timed_out = False
        self.cost: float | None = None
        self.log: list[dict[str, Any]] = []

    def counts(self) -> dict[str, Any]:
        return {"calls": self.calls, "cost": self.cost, "empty": self.empty, "log": tuple(self.log)}

    def _note(self, ordinal: int, args: Mapping[str, Any], reply: Any, outcome: str) -> None:
        record = _call_record(ordinal, args, reply, outcome)
        self.log.append(record)
        _log.info("nx_answer_pool_filter_call", **record)

    def counting(self, check: Callable[..., Awaitable[Mapping[str, Any]]]) -> Callable[..., Awaitable[Mapping[str, Any]]]:
        async def counted(args: dict[str, Any], *, timeout: float) -> Mapping[str, Any]:
            self.calls += 1
            ordinal = self.calls  # calls run side by side: the count may have moved on by the time this one ends
            try:
                reply = await check(args, timeout=timeout)
            except (TimeoutError, asyncio.CancelledError):
                self.timed_out = True
                self._note(ordinal, args, None, "timeout")
                raise
            except Exception:
                self._note(ordinal, args, None, "raised")
                raise
            outcome = _outcome(reply)
            if outcome == "empty":
                self.empty += 1
            self._note(ordinal, args, reply, outcome)
            return reply

        return counted


def _fields(
    *, scanned: int = 0, candidates: Sequence[str] = (), kept: Sequence[str] = (), calls: int = 0, cost: float | None = None,
    reason: str = "", need_s: float | None = None, room_s: float | None = None, empty: int = 0, log: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    kept_set = set(kept)
    return {
        "pool_key_scan": True, "pool_scanned": scanned, "pool_candidate_keys": tuple(candidates), "pool_keys_kept": tuple(kept),
        "pool_keys_rejected": tuple(k for k in candidates if k not in kept_set), "pool_filter_calls": calls,
        "pool_filter_cost_usd": cost, "pool_reason": reason, "pool_need_s": need_s, "pool_room_s": room_s,
        "pool_filter_empty_calls": empty, "pool_filter_log": tuple(log),
    }


def _with(base: Any, fields: Mapping[str, Any]) -> Any:
    """*base* (phase A's result) with the pool's report fields added and nothing else changed."""
    return replace(base, fields={**base.fields, **fields})


def _search_ids(view: BoundaryView) -> Mapping[str, Any] | None:
    """The first step's output whose tool is ``search`` and which carries ``ids``, or ``None``."""
    for i, out in enumerate(view.step_outputs):
        tool = view.tools[i] if i < len(view.tools) else ""
        if tool == "search" and isinstance(out, Mapping) and isinstance(out.get("ids"), list):
            return out
    return None


def _filter_end(env: PoolEnv) -> float:
    return env.fetch_until - _TAIL_S


def _need_s(first: CoverageResult, env: PoolEnv) -> float:
    """Seconds one assessment call may need: the boundary's minimum, or the first assessment's recorded latency per wave of
    its concurrent calls when that is longer."""
    a = first.ledger.assessment
    waves = max(1, math.ceil(a.calls / max(1, env.max_concurrent_calls)))
    return max(env.min_call_s, a.latency_ms / 1000.0 / waves)


def _short(view: BoundaryView, env: PoolEnv, need_s: float) -> tuple[str, float]:
    """``(reason, room_s)``: the skip reason when one assessment call does not fit beside the reassessment's reserve (else
    ``""``), and the seconds of room the filter had, which the report names beside the seconds one call needs."""
    room = _filter_end(env) - time.monotonic()
    remaining = view.remaining_seconds()
    if remaining is not None:
        room = min(room, remaining)
    usd = view.remaining_usd()
    if usd is not None:
        left = usd - (env.usd_reserve or 0.0)
        estimate = view.assessment_cost_estimate()
        if left <= 0 or (estimate is not None and left < estimate):
            return "filter_skipped_budget", room
    return ("filter_skipped_deadline" if room < need_s else ""), room


def _verdict(report: AssessmentReport, tally: _Tally) -> str:
    """The reason for a filter that did not run clean, or ``""``. A call that judged its chunks and cited none is a verdict
    (its reply held a record, none of them ``supports``); an empty reply is a failed call, as the assessment's instruction
    says, and ``filter_empty_reply`` names it when every call that did not complete was empty."""
    bad = report.raised_calls + report.unusable_calls
    skipped = report.skipped_budget_calls + report.skipped_deadline_calls
    verdicts = report.calls - bad
    if bad <= 0 and skipped == 0:
        return ""
    if verdicts > 0:
        return "filter_partial_failure"
    if tally.timed_out:
        return "filter_timeout"
    if report.calls == 0:
        return "filter_skipped_budget" if report.skipped_budget_calls else "filter_skipped_deadline"
    if tally.empty and report.raised_calls == 0 and report.unusable_calls == tally.empty:
        return "filter_empty_reply"
    return "filter_failed"


def _merge(base: Any, joined: Any, fields: Mapping[str, Any]) -> Any:
    """Phase A's result with phase B's joined items, keys and report fields after it. *joined* is phase B's own
    resolution. ``anchor_key_ids`` is phase A's alone."""
    f = dict(base.fields)
    part = joined.fields
    f["resolution_entered"] = True
    f.pop("resolution_reason", None)
    f["resolution_tokens"] = int(f.get("resolution_tokens", 0)) + int(part.get("resolution_tokens", 0))
    for name in ("resolved", "missing", "ambiguous", "joined_dropped", "tokens_dropped"):
        f[name] = (*tuple(f.get(name, ())), *tuple(part.get(name, ())))
    f.update(fields)
    detail = f"{base.detail}; pool: {joined.detail}" if base.detail else joined.detail
    return replace(
        base, fields=f, entered=True, items=[*base.items, *joined.items], keys=(*base.keys, *joined.keys), detail=detail)


async def run_pool_phase(view: BoundaryView, first: CoverageResult, base: Any, env: PoolEnv, *, anchor_keys: frozenset[str]) -> Any:
    """Phase B: scan the search step's non-anchor chunks for entity keys, keep the keys the assessment cites as support, and
    resolve and join them after phase A's *base* result. Never raises, and never undoes *base*."""
    tally = _Tally()
    try:
        return await _phase(view, first, base, env, anchor_keys, tally)
    except Exception as exc:  # noqa: BLE001 — a failed pool phase leaves phase A's result as it was
        _log.warning("nx_answer_pool_phase_error", error=type(exc).__name__)
        return _with(base, _fields(reason="pool_error", **tally.counts()))


async def _phase(
    view: BoundaryView, first: CoverageResult, base: Any, env: PoolEnv, anchor_keys: frozenset[str], tally: _Tally,
) -> Any:
    from nexus.entity_keys import find_key_tokens  # noqa: PLC0415 — deferred: the ledger stays light at import

    if base.entered and base.failure:
        return _with(base, _fields(reason="resolution_failed"))
    # phase A's tokens and items hold their slots; the pool's keys take what the cap leaves
    room = env.cap - max(int(base.fields.get("resolution_tokens", 0)), len(base.items))
    if room <= 0:  # no key the pool could add would be resolved, so no fetch and no model call is spent on it
        return _with(base, _fields(reason="cap_full"))
    out = _search_ids(view)
    if out is None:
        return _with(base, _fields(reason="no_search_step"))
    held = {a.chash.lower() for a in first.ledger.anchors if a.chash}
    hits = [h for h in env.hits_of(out) if (h.chash or h.id).lower() not in held and env.in_scope(h)]
    if not hits:
        return _with(base, _fields())
    if view.fetch is None:
        return _with(base, _fields(reason="no_fetch"))
    need_s = _need_s(first, env)
    why, room_s = _short(view, env, need_s)
    if why:  # nothing is started that one assessment call could not follow
        return _with(base, _fields(reason=why, need_s=round(need_s, 3), room_s=round(room_s, 3)))
    try:
        fetched = await view.fetch(hits, timeout=max(MIN_REPAIR_QUERY_S, (env.fetch_until - time.monotonic()) * FETCH_TIME_SHARE))
    except Exception as exc:  # noqa: BLE001 — a failed pool fetch leaves the anchors' keys as phase A resolved them
        _log.warning("nx_answer_pool_fetch_failed", error=type(exc).__name__)
        return _with(base, _fields(reason="fetch_failed"))
    held_texts = {strip_source_note(t) for t in view.evidence_texts if isinstance(t, str)}
    pool: list[tuple[EvidenceAnchor, tuple[str, ...]]] = []  # a chunk with a key, in fetch order
    scanned = 0
    for item in fetched:
        bare = strip_source_note(item.text) if item.text else ""
        if not bare or bare in held_texts:  # empty, or already one of the generator's inputs
            continue
        held_texts.add(bare)
        scanned += 1
        keys = tuple(dict.fromkeys(find_key_tokens(bare, item.collection, config=env.config)))
        if keys:
            anchor = EvidenceAnchor(
                f"{_POOL_ID_PREFIX}{len(pool) + 1}", "vector", "chunk", (item.chash or hashlib.sha256(bare.encode("utf-8")).hexdigest()).lower(),
                item.collection, item.tumbler, "", "", item.text)
            pool.append((anchor, keys))
    candidates = tuple(dict.fromkeys(k for _, keys in pool for k in keys if k not in anchor_keys))
    if not candidates:
        return _with(base, _fields(scanned=scanned))

    def closed(reason: str, room_s: float | None = None) -> Any:
        need = None if room_s is None else round(need_s, 3)
        return _with(base, _fields(
            scanned=scanned, candidates=candidates, reason=reason, need_s=need,
            room_s=None if room_s is None else round(room_s, 3), **tally.counts()))

    if not env.facets:
        return closed("no_facets")
    why, room_s = _short(view, env, need_s)
    if why:
        return closed(why, room_s)
    cand_set = set(candidates)
    carriers = [a for a, keys in pool if any(k in cand_set for k in keys)]

    def seconds_left() -> float | None:
        slice_s = max(0.0, _filter_end(env) - time.monotonic())
        remaining = view.remaining_seconds()
        return slice_s if remaining is None else min(remaining, slice_s)

    def usd_left() -> float | None:
        usd = view.remaining_usd()
        return None if usd is None else usd - (env.usd_reserve or 0.0)

    fview = replace(view, check=tally.counting(view.check), remaining_seconds=seconds_left, remaining_usd=usd_left)
    supported: set[str] = set()  # the item ids a ``supports`` record cites
    spent0 = view.assessment_cost_spent()
    report: AssessmentReport | None = None
    reason = ""
    try:
        _findings, report = await asyncio.wait_for(
            env.assess(list(env.facets), carriers, fview, 0.0, supported),
            timeout=max(MIN_REPAIR_QUERY_S, _filter_end(env) - time.monotonic() + MIN_REPAIR_QUERY_S))
    except TimeoutError:
        tally.timed_out = True
    except Exception as exc:  # noqa: BLE001 — a failed filter keeps no pool key; phase A's keys stand
        _log.warning("nx_answer_pool_filter_failed", error=type(exc).__name__)
    spent1 = view.assessment_cost_spent()
    tally.cost = None if spent1 is None else round(max(0.0, spent1 - (spent0 or 0.0)), 6)
    if report is None:
        supported.clear()
        reason = "filter_timeout" if tally.timed_out else "filter_failed"
    else:
        reason = _verdict(report, tally)
        if not reason and report.unread_ids:
            reason = "filter_unread"  # the size cap left some candidate chunks out of every call; their keys are rejected
    kept_chunks = [(a, keys) for a, keys in pool if a.item_id in supported]
    kept_keys = {k for _, keys in kept_chunks for k in keys if k in cand_set}
    kept = tuple(k for k in candidates if k in kept_keys)
    if tally.empty:
        _log.warning("nx_answer_pool_filter_empty_reply", empty_calls=tally.empty, calls=tally.calls, reason=reason)
    fields = _fields(scanned=scanned, candidates=candidates, kept=kept, reason=reason, **tally.counts())
    if not kept:
        return _with(base, fields)
    order = {k: i for i, k in enumerate(kept)}
    pairs = sorted(((a, k) for a, keys in kept_chunks for k in keys if k in kept_keys), key=lambda p: order[p[1]])
    # the overflow past the room the cap leaves is named in tokens_dropped by the join
    joined = await env.join(
        view, pairs, {}, cap=room, resolve_until=env.fetch_until - 2 * MIN_REPAIR_QUERY_S, fetch_until=env.fetch_until)
    if joined.failure:  # the kept keys joined nothing: none is kept, and the reason overrides an earlier one
        _log.warning("nx_answer_pool_resolve_failed", why=joined.failure[:80])
        return _with(base, _fields(scanned=scanned, candidates=candidates, reason="pool_resolve_failed", **tally.counts()))
    return _merge(base, joined, fields)
