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
contradicting ids, so those are not read). The filter's calls carry ``POOL_FILTER_KEY``, so the runner sends them on
``model_tiers.POOL_FILTER_ALIAS``, not the check tier's model (nexus-w8d30). Keys the anchors name are not filtered. Without the filter a pool key is projected
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
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import structlog

from nexus.plans.coverage_ledger import _ROLES, _TAG, AssessmentReport, BoundaryView, CoverageResult, EvidenceAnchor, FacetFinding
from nexus.plans.coverage_repair import MIN_REPAIR_QUERY_S, RepairHit
from nexus.plans.request_coverage import Facet
from nexus.plans.reserved_stream import strip_source_note

_log = structlog.get_logger(__name__)

__all__ = ["DISPATCH_USAGE_KEY", "POOL_FILTER_KEY", "POOL_REASONS", "PoolEnv", "run_pool_phase"]

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


#: The ``supports`` pairs one call's log entry lists; the rest are counted in ``supports_truncated``.
_CALL_SUPPORTS_CAP = 50
#: Longest string any log field carries: a backstop, since the ids are matched against the call's own and the rest are ours.
_FIELD_CAP = 80
#: The key the runner's assessment check puts the call's token counts under (a runner-owned key, never read from the model's
#: reply), and the attribute it gives an exception raised by a call. Only a run with the pool switch on carries it.
DISPATCH_USAGE_KEY = "_nx_dispatch_usage"

#: The arg the filter puts on each of its assessment calls (nexus-w8d30). The runner's coverage check removes it before the
#: dispatch and sends the call on the pool filter's model (``model_tiers.POOL_FILTER_ALIAS``); the operator never sees it.
POOL_FILTER_KEY = "_nx_pool_filter"
_USAGE_INTS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cap(value: Any, n: int = _FIELD_CAP) -> str:
    return str(value)[:n]


def _host_rejected(reply: Any) -> int:
    """How many records the host dropped from the reply (``bind`` lists an item id outside the call's items in ``rejected``)."""
    rejected = reply.get("rejected") if isinstance(reply, Mapping) else None
    return len(rejected) if isinstance(rejected, list) else 0


def _is_empty(reply: Any) -> bool:
    """An evidence list with no record in it and nothing the host dropped: a failed call. Plain checks only, because the
    filter's verdict reads this and logging must never move it."""
    evidence = reply.get("evidence") if isinstance(reply, Mapping) else None
    return isinstance(evidence, list) and not any(isinstance(r, Mapping) for r in evidence) and _host_rejected(reply) == 0


_FACET_LINE = re.compile(r"^\[([A-Za-z0-9_.\-]+)\]", re.MULTILINE)


def _call_scope(args: Mapping[str, Any]) -> tuple[frozenset[str], dict[str, str]]:
    """The item ids the call showed and its facet tags (lower case to the id), read from what was sent: the ledger's own
    rules for a valid record (``CoverageBoundary._assess``) are tested against these."""
    try:
        shown = json.loads(str(args.get("items", "")))
        ids = frozenset(str(i["id"]) for i in shown if isinstance(i, Mapping) and "id" in i)
    except (ValueError, TypeError):
        ids = frozenset()
    instruction = str(args.get("check_instruction", ""))
    tags = {t.lower(): t for t in _FACET_LINE.findall(instruction.partition("Facets:\n")[2])}
    return ids, tags


def _accepted(rec: Mapping[str, Any], ids: frozenset[str], tags: Mapping[str, str]) -> tuple[str, str, str] | None:
    """``(item id, facet id, role)`` when the ledger would accept *rec* for this call, else ``None``."""
    item_id, role, quote = rec.get("item_id"), rec.get("role"), rec.get("quote")
    m = _TAG.match(quote) if isinstance(quote, str) else None
    facet = tags.get(m.group(1).lower()) if m is not None else None
    if not isinstance(item_id, str) or item_id not in ids or role not in _ROLES or facet is None:
        return None
    return item_id, facet, role


def _reply_body(reply: Any) -> Any:
    """What the model itself said: its ``evidence`` list. A reply without one is hashed whole, less the keys that are not the
    model's (the runner's usage and ``ok``), so the same reply hashes the same on every pass whatever the call cost."""
    evidence = reply.get("evidence") if isinstance(reply, Mapping) else None
    if isinstance(evidence, list):
        return evidence
    if isinstance(reply, Mapping):
        return {k: v for k, v in reply.items() if k not in (DISPATCH_USAGE_KEY, "usage", "ok")}
    return reply


def _usage_of(reply: Any, exc: BaseException | None) -> dict[str, Any]:
    """The runner's token counts for the call, from its own key (never a model-supplied ``usage``); every field ``None`` when
    the call reported none."""
    raw = getattr(exc, DISPATCH_USAGE_KEY, None) if exc is not None else (reply.get(DISPATCH_USAGE_KEY) if isinstance(reply, Mapping) else None)
    raw = raw if isinstance(raw, Mapping) else {}
    out: dict[str, Any] = {n: (raw[n] if isinstance(raw.get(n), int) and not isinstance(raw.get(n), bool) else None) for n in _USAGE_INTS}
    out["model"] = _cap(raw["model"]) if isinstance(raw.get("model"), str) else None
    return out


def _call_record(ordinal: int, args: Mapping[str, Any], reply: Any, exc: BaseException | None = None, *, timed_out: bool = False) -> dict[str, Any]:
    """What one filter call sent and got back, as closed names, hashes and ids: no chunk text, no quote.

    ``outcome`` agrees with the verdict: ``supports`` and ``no_support`` completed (at least one record the ledger accepts,
    with and without a ``supports`` among them; ``no_support`` is the genuine reject), ``empty`` (an evidence list with no
    record), ``all_rejected`` (records, or records the host dropped, and none accepted), ``malformed`` (not the check
    shape), ``raised`` or ``timeout``, the last two with ``error``, the exception's class name. ``supports`` holds only
    the item and facet ids the ledger accepts; ``unmatched`` counts the records it would not and ``host_rejected`` those the
    host dropped before the pool saw the reply. ``payload_sha256`` covers the items and the instruction the boundary sends: the
    operator's own prompt wrapper is built below it and is not seen here."""
    payload = f"{args.get('items', '')}\n{args.get('check_instruction', '')}"
    record: dict[str, Any] = {
        "call": ordinal, "payload_sha256": _digest(payload), "reply_chars": None, "reply_sha256": None,
        **_usage_of(reply, exc), "supports": [], "supports_truncated": 0, "unmatched": 0, "host_rejected": 0,
        "outcome": "timeout" if timed_out else "raised", "error": _cap(type(exc).__name__) if exc is not None else None,
    }
    if exc is not None:
        return record
    raw = json.dumps(_reply_body(reply), sort_keys=True, default=str, ensure_ascii=False)
    record["reply_chars"], record["reply_sha256"] = len(raw), _digest(raw)
    evidence = reply.get("evidence") if isinstance(reply, Mapping) else None
    if not isinstance(evidence, list):
        record["outcome"] = "malformed"
        return record
    ids, tags = _call_scope(args)
    records = [r for r in evidence if isinstance(r, Mapping)]
    accepted = [a for r in records if (a := _accepted(r, ids, tags)) is not None]
    cited = list(dict.fromkeys((i, f) for i, f, role in accepted if role == "supports"))
    record["supports"] = [{"item_id": i, "facet_id": f} for i, f in cited[:_CALL_SUPPORTS_CAP]]
    record["supports_truncated"] = max(0, len(cited) - _CALL_SUPPORTS_CAP)
    record["unmatched"] = len(records) - len(accepted)
    record["host_rejected"] = _host_rejected(reply)
    if accepted:
        record["outcome"] = "supports" if cited else "no_support"
    else:
        record["outcome"] = "all_rejected" if records or record["host_rejected"] else "empty"
    return record


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

    def _note(self, ordinal: int, args: Mapping[str, Any], reply: Any, exc: BaseException | None = None, *, timed_out: bool = False) -> None:
        """One log entry and one event for a call. Never raises: logging must not move a verdict."""
        try:
            record = _call_record(ordinal, args, reply, exc, timed_out=timed_out)
        except Exception as err:  # noqa: BLE001 — a call that cannot be described is named, and the filter carries on
            record = {"call": ordinal, "outcome": "unlogged", "error": _cap(type(err).__name__)}
            _log.warning("nx_answer_pool_filter_log_failed", call=ordinal, error=record["error"])
        self.log.append(record)
        try:
            _log.info("nx_answer_pool_filter_call", **record)
        except Exception:  # noqa: BLE001, S110 — see above
            pass

    def counting(self, check: Callable[..., Awaitable[Mapping[str, Any]]]) -> Callable[..., Awaitable[Mapping[str, Any]]]:
        async def counted(args: dict[str, Any], *, timeout: float) -> Mapping[str, Any]:
            self.calls += 1
            ordinal = self.calls  # calls run side by side: the count may have moved on by the time this one ends
            try:
                reply = await check({**args, POOL_FILTER_KEY: True}, timeout=timeout)
            except (TimeoutError, asyncio.CancelledError) as exc:
                self.timed_out = True
                self._note(ordinal, args, None, exc, timed_out=True)
                raise
            except Exception as exc:
                cut = any(c.__name__ == "OperatorTimeoutError" for c in type(exc).__mro__)  # the dispatcher's own timeout
                self.timed_out = self.timed_out or cut
                self._note(ordinal, args, None, exc, timed_out=cut)
                raise
            if _is_empty(reply):
                self.empty += 1
            self._note(ordinal, args, reply)
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
