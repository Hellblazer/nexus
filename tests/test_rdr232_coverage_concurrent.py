# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 (nexus-jypmj.17, nexus-jypmj.3.10): the coverage assessment runs its calls concurrently.

The 2026-10-28 live canary measured +118 s with the coverage check on: ``CoverageBoundary._assess`` made one
``operator_check`` call per evidence stream serially, the 65-anchor graph call hit the 60 s call timeout, and a
raised call stopped the loop. These tests pin the replacement: the calls of a stage run at most
``MAX_CONCURRENT_ASSESSMENT_CALLS`` at a time inside the stage's share of the deadline, a call that raises or replies
unusably costs only its own facets, a stream over ``MAX_ANCHORS_PER_ASSESSMENT_CALL`` anchors is split into packs,
the ledger and the call log are in plan order whatever order the calls finish in, and the terminal step's
``elapsed_ms`` no longer carries the assessment.

The timing model is the canary at 1/100 scale: a call takes ``BASE_S + PER_ANCHOR_S * anchors`` (0.15 s + 7 ms per
anchor here; 15 s + 0.7 s per anchor in the canary, so 10-15 anchors answer in about 20-25 s and 65 anchors need 60.5 s,
past the 60 s timeout), and a call longer than its ``timeout`` argument is cut the way the dispatcher cuts it.
Real ``asyncio.sleep`` is used; every bound below is a lower bound of the model or a generous multiple of it.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import patch

import pytest
import structlog

from nexus.operators.llm_api import DEFAULT_MAX_CONCURRENCY
from nexus.operators import llm_types
from nexus.operators.llm_types import DispatchUsage
from nexus.plans import coverage_ledger
from nexus.plans.coverage_ledger import (
    MAX_ANCHORS_PER_ASSESSMENT_CALL,
    MAX_CONCURRENT_ASSESSMENT_CALLS,
    UNASSESSED_CAUSES,
    BoundaryView,
    CoverageBoundary,
    CoverageResult,
    _balanced_chunks,
)
from nexus.plans.run_summary import RunSummaryInputs, build_run_summary
from nexus.plans.runner import plan_run
from tests.test_rdr232_coverage_ledger import (
    CODE_COLL,
    PLAN,
    RDR_COLL,
    Dispatcher,
    _cov,
    _h,
    _match,
    _request,
    _reserved,
    _run,
    _structured,
    _tag_lines,
    supports_nothing,
)

_log = structlog.get_logger(__name__)

BASE_S = 0.15
PER_ANCHOR_S = 0.007
SCALE = 100  # one second here is one hundred in the canary
CALL_TIMEOUT_S = 0.6  # ASSESSMENT_CALL_TIMEOUT_S (60 s) at 1/100


def _latency(anchors: int) -> float:
    return BASE_S + PER_ANCHOR_S * anchors


def _stream_of_items(items: str) -> str:
    """The stream a call reads, from its first item's text: the Canary's vector, graph and other items are named for it,
    anything else is the reserved (primary) stream's."""
    head = json.loads(items)[0]["text"].split()[0]
    return head if head in ("vector", "graph", "other") else "primary"


def _reply(items: str, instruction: str, *, role: str = "supports") -> dict:
    first = json.loads(items)[0]["id"]
    return {"ok": True, "evidence": [{"item_id": first, "quote": f"[{t}] a short quote", "role": role} for t in _tag_lines(instruction)]}


class Canary(Dispatcher):
    """The canary's evidence: a reserved primary stream of 12, a vector stream of 12, a graph stream of 65 and 10
    unattributed items, 99 in all; every ``operator_check`` goes through ``self.model`` (an async callable)."""

    def __init__(self, model, *, vector: int = 12, graph: int = 65, other: int = 10) -> None:
        super().__init__(hydrates=True)
        self.model = model
        self.vec = [f"vector hit {i}" for i in range(vector)]
        self.gr = [f"graph hit {i}" for i in range(graph)]
        self.oth = [f"other hit {i}" for i in range(other)]
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "search":
            self.calls.append((tool, dict(args)))
            return _structured(self.vec, RDR_COLL)
        if tool == "traverse":
            self.calls.append((tool, dict(args)))
            return _structured(self.gr, CODE_COLL) | {"traversal": {"policy_applied": True, "widened": None}}
        if tool == "store_get_many":
            self.calls.append((tool, dict(args)))
            return {"contents": [*self.vec, *self.gr, *self.oth]}
        if tool == "operator_check":
            self.calls.append((tool, dict(args)))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                return await self.model(args)
            finally:
                self.in_flight -= 1
        return await super().__call__(tool, args)


async def modelled(args: dict) -> dict:
    """The scaled canary latency model: sleep ``_latency(anchors)``, or run into the call's own timeout."""
    n = len(json.loads(args["items"]))
    need = _latency(n)
    if need > args["timeout"]:
        await asyncio.sleep(args["timeout"])
        raise TimeoutError("the call outran its timeout")
    await asyncio.sleep(need)
    return _reply(args["items"], args["check_instruction"])


def _boundary(**kw: Any) -> CoverageBoundary:
    return CoverageBoundary(request=_request(1), call_timeout_s=CALL_TIMEOUT_S, min_call_s=0.01, **kw)


async def _timed(d: Dispatcher, boundary: CoverageBoundary, **kw: Any) -> tuple[CoverageResult, float]:
    started = time.monotonic()
    result = await _run(d, boundary, reserved_stream=_reserved(n=12), **kw)
    return _cov(result), time.monotonic() - started


# ── the timing model: before and after ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_canary_before_serial_one_pack_per_stream_runs_the_sum_and_the_graph_call_times_out() -> None:
    """The measured shape, reproduced with the new knobs set to the old behaviour (one call at a time, no split):
    four calls in a row, the 65-anchor one cut at the timeout, the stage the sum of its calls."""
    d = Canary(modelled)
    cov, elapsed = await _timed(d, _boundary(max_concurrent_calls=1, max_anchors_per_call=10**6))
    report = cov.ledger.assessment
    assert report.streams == ("primary", "vector", "graph", "other") and report.calls == 4
    assert report.raised_calls == 1 and report.reply_by_stream()["graph"]["raised"] == 1
    floor = _latency(12) + _latency(12) + CALL_TIMEOUT_S + _latency(10)
    assert elapsed >= floor, "serial calls add up"
    assert d.max_in_flight == 1
    _log.info("rdr232_canary_model_before", scaled_s=round(elapsed * SCALE), floor_s=round(floor * SCALE), measured_live_s=118)


@pytest.mark.asyncio
async def test_the_canary_after_runs_in_about_two_call_times_and_the_graph_pack_is_split() -> None:
    d = Canary(modelled)
    serial_floor = _latency(12) + _latency(12) + CALL_TIMEOUT_S + _latency(10)
    cov, elapsed = await _timed(d, _boundary())
    report = cov.ledger.assessment
    assert report.calls == 7, "primary, vector, other and the graph stream in four packs"
    assert report.raised_calls == 0 and report.outcome == "ok", "no call outran its timeout once the graph stream is split"
    assert sorted(report.facets_per_call) == [1] * 7
    assert d.max_in_flight == MAX_CONCURRENT_ASSESSMENT_CALLS
    two_calls = 2 * _latency(MAX_ANCHORS_PER_ASSESSMENT_CALL)
    assert elapsed < two_calls + 0.25, f"7 calls in 2 waves of at most 4: {elapsed:.2f} s against {two_calls:.2f} s"
    assert elapsed < serial_floor * 0.6
    _log.info("rdr232_canary_model_after", scaled_s=round(elapsed * SCALE), serial_floor_s=round(serial_floor * SCALE))


@pytest.mark.asyncio
async def test_n_slow_calls_finish_in_about_one_call_time_not_n() -> None:
    """Four equal slow calls (two streams, two facet groups) at concurrency 4: one call's time. Failing before: they
    ran one after the other and took four."""
    delay = 0.3
    d = Canary(lambda args: _slow(args, delay), vector=5, graph=5, other=0)
    boundary = CoverageBoundary(request=_request(2), max_facets_per_call=1, call_timeout_s=5.0)
    started = time.monotonic()
    result = await _run(d, boundary)
    elapsed = time.monotonic() - started
    report = _cov(result).ledger.assessment
    assert report.calls == 4 and report.streams == ("vector", "graph")
    assert d.max_in_flight == 4
    assert delay <= report.latency_ms / 1000 < 2 * delay, f"4 calls of {delay} s took {report.latency_ms} ms (serial: {4 * delay} s)"
    assert elapsed < 4 * delay


async def _slow(args: dict, delay: float) -> dict:
    await asyncio.sleep(delay)
    return _reply(args["items"], args["check_instruction"])


@pytest.mark.asyncio
async def test_concurrency_is_bounded_by_the_boundary_setting() -> None:
    d = Canary(lambda args: _slow(args, 0.05), vector=3, graph=3, other=0)
    boundary = CoverageBoundary(request=_request(4), max_facets_per_call=1, max_concurrent_calls=2, call_timeout_s=5.0)
    cov, _ = await _timed(d, boundary)
    assert cov.ledger.assessment.calls == 12  # primary, vector, graph x 4 facets
    assert d.max_in_flight == 2


def test_the_concurrency_bound_is_at_most_the_dispatchers_own_gate() -> None:
    """Why 4: the direct-API backend gates HTTP attempts at ``NX_LLM_MAX_CONCURRENCY`` (default 4) and a call past it
    waits for a permit inside its own timeout. More than that default only queues."""
    assert 1 <= MAX_CONCURRENT_ASSESSMENT_CALLS <= DEFAULT_MAX_CONCURRENCY
    assert CoverageBoundary(request=None).max_concurrent_calls == MAX_CONCURRENT_ASSESSMENT_CALLS


# ── the stage never exceeds its share ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_check_that_ignores_its_timeout_is_cut_at_the_stage_share() -> None:
    """The stage may use 0.4 of the deadline left (here 1.0 s, so 0.4 s). A check that sleeps 5 s whatever it is told
    is cut from outside at the stage end, and the calls that never started are skipped, not queued past it."""
    async def ignores_timeout(args: dict) -> dict:  # noqa: ARG001
        await asyncio.sleep(5.0)
        return {}

    d = Canary(ignores_timeout, vector=3, graph=3, other=0)
    boundary = CoverageBoundary(request=_request(1), call_timeout_s=10.0, min_call_s=0.01, max_concurrent_calls=2)
    started = time.monotonic()
    result = await _run(d, boundary, reserved_stream=_reserved(n=2), deadline=started + 1.0)
    cov = _cov(result)
    report = cov.ledger.assessment
    share = 0.4
    assert report.latency_ms <= (share + 0.15) * 1000, f"the stage took {report.latency_ms} ms of a {share} s share"
    assert report.raised_calls == 2 and report.calls == 2
    steps = [r for r in result.step_records if r.operator == "operator_check"]
    assert len(steps) == 2 and all(not r.ok for r in steps), "a cut call is still a StepRecord"
    # the third stream's call never started: its facet is already unassessed on the first call's cause
    assert [f.state for f in cov.ledger.findings] == ["unassessed"]


@pytest.mark.asyncio
async def test_queued_calls_draw_their_timeout_from_what_is_left_of_the_stage_when_they_start() -> None:
    """Six calls, two at a time, 0.3 s each, a 0.4 s stage: the first wave completes, the second starts at 0.3 s with
    0.1 s left and is cut at the stage end, the third wave is skipped for the deadline."""
    seen: list[float] = []

    async def record_timeout(args: dict) -> dict:
        seen.append(args["timeout"])
        return await _slow(args, 0.3)

    d = Canary(record_timeout, vector=3, graph=3, other=0)
    boundary = CoverageBoundary(request=_request(3), max_facets_per_call=1, call_timeout_s=10.0, min_call_s=0.05, max_concurrent_calls=2)
    started = time.monotonic()
    result = await _run(d, boundary, deadline=started + 1.0)
    report = _cov(result).ledger.assessment
    assert max(seen) <= 0.4 + 1e-3, "no call is given more than the stage allowance"
    assert seen[2] < 0.15 and seen[3] < 0.15, "the second wave drew what was left, not the call cap"
    assert report.latency_ms <= 550
    assert report.outcome == "partial_failure"
    assert report.calls == 4 < 6, "the third wave had no time left and was skipped, not started"
    assert report.raised_calls == 2, "the second wave was cut at the stage end"


# ── one failed call costs only its own facets ───────────────────────────────────────────────────────────────


def _by_stream(**handlers: Any):
    async def model(args: dict) -> Any:
        handler = handlers.get(_stream_of_items(args["items"]), supports_nothing)  # a stream not named reads and finds nothing
        out = handler(args["items"], args["check_instruction"])
        if isinstance(out, BaseException):
            raise out
        return out
    return model


def _reasons(cov: CoverageResult) -> dict[str, str]:
    return {f.facet_id: f.reason for f in cov.ledger.findings if f.state == "unassessed"}


def _summary(cov: CoverageResult) -> dict[str, Any]:
    return build_run_summary(
        RunSummaryInputs(request=None), outcome="ok", plan_id=7, step_count=5, duration_ms=100, cost_usd=None,
        trace=True, coverage=cov, terminal_reached=True,
    )


@pytest.mark.asyncio
async def test_a_raised_call_leaves_only_its_own_facets_unassessed_and_the_others_still_count() -> None:
    """Three facets, two per call: the vector call over f3 raises. Failing before: the first raise stopped the pass,
    so f1/f2 were never read on the graph stream and f3 was not the only unassessed facet."""
    def vector(items: str, instruction: str):
        return RuntimeError("down") if "f3" in _tag_lines(instruction) else supports_nothing(items, instruction)

    d = Canary(_by_stream(vector=vector, graph=lambda i, s: supports_nothing(i, s), other=lambda i, s: supports_nothing(i, s)),
               vector=3, graph=3, other=0)
    cov, _ = await _timed(d, CoverageBoundary(request=_request(3), max_facets_per_call=2))
    n_checks = d.tools.count("operator_check")
    assert n_checks == cov.ledger.assessment.calls == 6, "every planned call was attempted: primary, vector and graph, two groups each"
    assert cov.ledger.assessment.raised_calls == 1
    assert _reasons(cov) == {"f3": "assessment_raised"}
    by = {f.facet_id: f.state for f in cov.ledger.findings}
    assert by["f1"] == by["f2"] == "unsupported", "read on every stream, nothing bears on them"
    s = _summary(cov)["coverage"]["unassessed_by_cause"]
    assert s["raised_call"] == 1 and s["stopped_after_failure"] == 0
    assert cov.ledger.assessment.outcome == "partial_failure", "five calls were read, one raised"


@pytest.mark.asyncio
async def test_a_malformed_reply_does_not_stop_the_other_calls_either() -> None:
    def vector(items: str, instruction: str):  # noqa: ARG001
        return {"ok": True}  # not the check shape

    d = Canary(_by_stream(vector=vector, graph=lambda i, s: supports_nothing(i, s), other=lambda i, s: supports_nothing(i, s)),
               vector=3, graph=3, other=0)
    cov, _ = await _timed(d, CoverageBoundary(request=_request(3), max_facets_per_call=2))
    assert cov.ledger.assessment.calls == 6
    assert _reasons(cov) == {"f1": "assessment_unusable", "f2": "assessment_unusable", "f3": "assessment_unusable"}
    cause = _summary(cov)["coverage"]["unassessed_by_cause"]
    assert cause["unusable_reply"] == 3 and cause["stopped_after_failure"] == 0


@pytest.mark.asyncio
async def test_another_stream_that_supports_covers_the_facet_a_failed_stream_could_not() -> None:
    d = Canary(_by_stream(vector=lambda i, s: RuntimeError("down"), graph=lambda i, s: _reply(i, s), other=lambda i, s: _reply(i, s)),
               vector=3, graph=3, other=0)
    cov, _ = await _timed(d, CoverageBoundary(request=_request(2)))
    assert [f.state for f in cov.ledger.findings] == ["covered", "covered"]
    assert cov.ledger.assessment.outcome == "partial_failure" and cov.ledger.assessment.raised_calls == 1
    assert cov.state == "complete"


def test_stopped_after_failure_is_kept_in_the_enumeration_but_no_assessment_emits_it() -> None:
    """The cause stays a member of the closed list (old envelopes carry it; the wire is additive), and ``other`` stays
    last. A pass no longer stops at a failure, so only a skipped call (budget, deadline) leaves a facet unread, and those
    have their own causes."""
    assert "stopped_after_failure" in UNASSESSED_CAUSES and UNASSESSED_CAUSES[-1] == "other"
    assert not hasattr(coverage_ledger.CoverageBoundary, "stop_after_failure")


# ── deterministic order whatever the completion order ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_ledger_and_the_call_log_are_in_plan_order_whatever_order_the_calls_finish_in() -> None:
    """Earlier calls are the slower ones, so they finish last. The call log, the reply classes by stream and the
    unassessed counts are the plan's order and the same as when the calls finish in plan order."""
    async def run(slow_first: bool) -> tuple[CoverageResult, list[str]]:
        finished: list[str] = []

        async def model(args: dict) -> dict:
            stream = _stream_of_items(args["items"])
            rank = {"primary": 0, "vector": 1, "graph": 2, "other": 3}.get(stream, 0)
            await asyncio.sleep(0.04 * (3 - rank if slow_first else rank))
            finished.append(stream)
            if stream == "vector":
                return _reply(args["items"], args["check_instruction"])
            if stream == "graph":
                return {"ok": False, "evidence": []}
            return supports_nothing(args["items"], args["check_instruction"])

        d = Canary(model, vector=3, graph=3, other=2)
        cov, _ = await _timed(d, CoverageBoundary(request=_request(2), call_timeout_s=5.0))
        return cov, finished

    slow, order_slow = await run(True)
    fast, order_fast = await run(False)
    assert order_slow[0] != order_fast[0], "the two runs really finished in different orders"
    a, b = slow.ledger.assessment, fast.ledger.assessment
    assert [c.stream for c in a.call_log] == ["primary", "vector", "graph", "other"]
    assert a.call_log == b.call_log and a.reply_by_stream() == b.reply_by_stream() and a.reply_classes() == b.reply_classes()
    assert list(a.reply_by_stream()) == ["primary", "vector", "graph", "other"]
    assert slow.ledger.findings == fast.ledger.findings
    assert _summary(slow)["coverage"]["unassessed_by_cause"] == _summary(fast)["coverage"]["unassessed_by_cause"]


# ── the large pack ──────────────────────────────────────────────────────────────────────────────────────────


def test_a_stream_is_split_into_the_fewest_balanced_packs_within_the_cap() -> None:
    assert [len(p) for p in _balanced_chunks(list(range(65)), 20)] == [17, 16, 16, 16]
    assert [len(p) for p in _balanced_chunks(list(range(20)), 20)] == [20]
    assert [len(p) for p in _balanced_chunks(list(range(21)), 20)] == [11, 10]
    assert _balanced_chunks([], 20) == []
    flat = [x for p in _balanced_chunks(list(range(65)), 20) for x in p]
    assert flat == list(range(65)), "order kept, nothing dropped or repeated"


@pytest.mark.asyncio
async def test_no_call_reads_more_than_the_pack_cap_and_every_anchor_is_read_once_per_group() -> None:
    """The canary's 65-anchor graph stream is four calls of 16-17, not one over the timeout. Failing before: one call
    carried all 65."""
    seen: list[list[str]] = []

    async def model(args: dict) -> dict:
        items = json.loads(args["items"])
        seen.append([i["text"] for i in items])
        return _reply(args["items"], args["check_instruction"])

    d = Canary(model)
    await _timed(d, CoverageBoundary(request=_request(1), call_timeout_s=5.0))
    assert max(len(s) for s in seen) <= MAX_ANCHORS_PER_ASSESSMENT_CALL
    graph = [s for s in seen if s[0].startswith("graph")]
    assert sorted(len(s) for s in graph) == [16, 16, 16, 17]
    assert sorted(t for s in graph for t in s) == sorted(d.gr), "every graph anchor in exactly one pack"
    assert len(d.gr) > MAX_ANCHORS_PER_ASSESSMENT_CALL, "non-vacuity: the stream really is over the cap"


@pytest.mark.asyncio
async def test_a_failed_pack_loses_its_own_anchors_not_the_stream() -> None:
    """The second graph pack raises; the fourth supports f1. f1 is covered by that evidence. With no supporting pack
    f1 is unassessed (a pack of the stream was never read) and the cause is the raised call."""
    calls = {"n": 0}

    async def second_graph_pack_raises(args: dict) -> dict:
        if _stream_of_items(args["items"]) == "graph":
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("down")
            if calls["n"] == 4:
                return _reply(args["items"], args["check_instruction"])
        return supports_nothing(args["items"], args["check_instruction"])

    d = Canary(second_graph_pack_raises)
    cov, _ = await _timed(d, CoverageBoundary(request=_request(1), call_timeout_s=5.0))
    assert [f.state for f in cov.ledger.findings] == ["covered"]
    assert cov.ledger.assessment.raised_calls == 1

    calls["n"] = 0

    async def no_support(args: dict) -> dict:
        if _stream_of_items(args["items"]) == "graph":
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("down")
        return supports_nothing(args["items"], args["check_instruction"])

    cov2, _ = await _timed(Canary(no_support), CoverageBoundary(request=_request(1), call_timeout_s=5.0))
    assert [(f.state, f.reason) for f in cov2.ledger.findings] == [("unassessed", "assessment_raised")]


# ── the USD allowance ───────────────────────────────────────────────────────────────────────────────────────


async def _direct_view(first_dispatcher: Dispatcher, check, **kw: Any) -> BoundaryView:
    first = await _run(first_dispatcher, CoverageBoundary(request=_request(1)))
    prior = _cov(first)
    return BoundaryView(
        evidence_texts=[a.content for a in prior.ledger.anchors], observed=True, step_outputs=list(first.steps),
        tools=["search", "traverse", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[], check=check,
        remaining_seconds=kw.pop("remaining_seconds", lambda: None), remaining_usd=kw.pop("remaining_usd", lambda: None), **kw,
    )


@pytest.mark.asyncio
async def test_a_spent_usd_allowance_starts_no_call() -> None:
    called = 0

    async def check(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        nonlocal called
        called += 1
        return _reply(args["items"], args["check_instruction"])

    view = await _direct_view(Dispatcher(), check, remaining_usd=lambda: 0.0)
    cov = await CoverageBoundary(request=_request(2)).evaluate(view)
    assert called == 0 and cov.ledger.assessment.outcome == "skipped_budget"
    assert _summary(cov)["coverage"]["unassessed_by_cause"]["budget_skip"] == 2


@pytest.mark.asyncio
async def test_calls_in_flight_are_counted_against_the_usd_allowance_at_the_mean_known_cost() -> None:
    """2.5 left, 1.0 a call: the first three start (2.5, 1.5, 0.5 left net of those in flight), the fourth would
    spend what is not there and is skipped, so the concurrent stage cannot overshoot by the whole wave."""
    started = 0

    async def check(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        nonlocal started
        started += 1
        await asyncio.sleep(0.05)
        return supports_nothing(args["items"], args["check_instruction"])

    view = await _direct_view(Dispatcher(), check, remaining_usd=lambda: 2.5, assessment_cost_estimate=lambda: 1.0)
    cov = await CoverageBoundary(request=_request(3), max_facets_per_call=1).evaluate(view)
    assert started == 3 == cov.ledger.assessment.calls
    assert cov.ledger.assessment.outcome == "partial_failure" and "USD" in cov.ledger.assessment.detail


# ── the runner: accounting and the terminal step's elapsed ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assessment_time_is_not_charged_to_the_terminal_step() -> None:
    """Failing before: the terminal step's ``elapsed_ms`` started before the boundary, so a 0.4 s assessment showed as
    0.4 s of generation in ``nx_answer_steps``. The assessment calls carry their own time."""
    async def slow_assessment(args: dict) -> dict:
        return await _slow(args, 0.4)

    d = Canary(slow_assessment, vector=3, graph=3, other=0)
    result = await _run(d, CoverageBoundary(request=_request(1), call_timeout_s=5.0))
    gen = [r for r in result.step_records if r.operator == "generate"]
    checks = [r for r in result.step_records if r.operator == "operator_check"]
    assert len(gen) == 1 and checks
    assert gen[0].elapsed_ms < 200, f"generation was charged {gen[0].elapsed_ms} ms"
    assert all(r.elapsed_ms >= 380 for r in checks)
    assert _cov(result).ledger.assessment.latency_ms >= 380, "the run summary's assessment_latency_ms still has it"


@pytest.mark.asyncio
async def test_assessment_time_is_not_charged_to_a_bundled_terminal_step_either() -> None:
    plan = {"steps": [
        PLAN["steps"][0], PLAN["steps"][1], PLAN["steps"][2],
        {"tool": "rank", "args": {"criterion": "c", "items": "$step3.contents"}},
        {"tool": "generate", "args": {"context": "$step4.ranked", "question": "q"}},
    ]}

    async def slow_assessment(args: dict) -> dict:
        return await _slow(args, 0.4)

    d = Canary(slow_assessment, vector=3, graph=3, other=0)

    async def fake_bundle(bundle, **kwargs):  # noqa: ARG001
        return {"text": "bundled answer"}

    class _Wrapper:
        supports_bundling = True

        async def __call__(self, tool: str, args: dict) -> dict:
            return await d(tool, args)

    with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
        result = await plan_run(_match(plan), {}, dispatcher=_Wrapper(), coverage_boundary=CoverageBoundary(request=_request(1), call_timeout_s=5.0))
    bundle_records = [r for r in result.step_records if r.operator not in ("operator_check", "search", "traverse", "store_get_many")]
    assert bundle_records and all(r.elapsed_ms < 200 for r in bundle_records), [(r.operator, r.elapsed_ms) for r in bundle_records]


@pytest.mark.asyncio
async def test_concurrent_calls_keep_their_own_usage_and_step_index() -> None:
    """Each concurrent call reads the ambient usage sink of its own task: its StepRecord carries its own cost, and the
    step indices are distinct and numbered past the plan."""
    costs = {"primary": 0.01, "vector": 0.02, "graph": 0.03}

    async def model(args: dict) -> dict:
        stream = _stream_of_items(args["items"])
        sink = llm_types._ambient_usage_sink.get()
        assert sink is not None
        await asyncio.sleep(0.05 if stream == "primary" else 0.02)  # interleave
        sink.append(DispatchUsage(
            model="haiku", cost_usd=costs.get(stream, 0.0), input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
            cache_read_input_tokens=0, duration_ms=1, duration_api_ms=1, num_turns=1,
        ))
        return _reply(args["items"], args["check_instruction"])

    d = Canary(model, vector=3, graph=3, other=0)
    result = await _run(d, CoverageBoundary(request=_request(1), call_timeout_s=5.0), reserved_stream=_reserved(n=2))
    checks = [r for r in result.step_records if r.operator == "operator_check"]
    assert len(checks) == 3
    assert sorted(r.cost_usd for r in checks) == [0.01, 0.02, 0.03]
    indices = [r.step_index for r in checks]
    assert len(set(indices)) == 3 and min(indices) >= len(PLAN["steps"])
